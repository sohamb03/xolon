import string
from time import sleep
from io import BytesIO
from base64 import b64encode
from qrcode import make as qrcode_make
from decimal import Decimal
from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
from datetime import datetime
from xolon.blueprints.wallet import wallet_bp
from xolon.decorators import check_confirmed
from xolon.library.docker import docker
from xolon.library.helpers import capture_event
from xolon.library.jsonrpc import Wallet, to_atomic
from xolon.library.cache import cache
from xolon.forms import Send, Delete, Restore, Secrets
from xolon.token import generate_token, validate_token
from xolon.factory import db, bcrypt
from xolon.models import User


@wallet_bp.route('/wallet/setup', methods=['GET', 'POST'])
@login_required
@check_confirmed
def setup():
    if current_user.wallet_created:
        return redirect(url_for('wallet.dashboard'))
    else:
        restore_form = Restore()
        if restore_form.validate_on_submit():
            c = docker.create_wallet(current_user.id, restore_form.seed.data)
            cache.store_data(f'init_wallet_{current_user.id}', 30, c)
            capture_event(current_user.id, 'restore_wallet')
            current_user.wallet_created = True
            db.session.commit()
            return redirect(url_for('wallet.loading'))
        else:
            return render_template(
                'wallet/setup.html',
                restore_form=restore_form
            )


@wallet_bp.route('/wallet/loading')
@login_required
@check_confirmed
def loading():
    if current_user.wallet_connected and current_user.wallet_created:
        return redirect(url_for('wallet.dashboard'))

    if current_user.wallet_created is False:
        return redirect(url_for('wallet.setup'))

    return render_template('wallet/loading.html')


@wallet_bp.route('/wallet/dashboard')
@login_required
@check_confirmed
def dashboard():
    send_form = Send()
    delete_form = Delete()
    _address_qr = BytesIO()
    all_transfers = list()
    wallet = Wallet(
        proto='http',
        host='127.0.0.1',
        port=current_user.wallet_port,
        username=current_user.id,
        password=current_user.wallet_password
    )
    if not docker.container_exists(current_user.wallet_container):
        current_user.clear_wallet_data()
        return redirect(url_for('wallet.loading'))

    if not wallet.connected:
        sleep(1.5)
        return redirect(url_for('wallet.loading'))

    address = wallet.get_address()

    transfers = wallet.get_transfers()
    for _type in transfers:
        for tx in transfers[_type]:
            all_transfers.append(tx)

    balances = wallet.get_balances()

    qr_uri = f'xolentum:{address}?tx_description={current_user.email}'
    qrcode_make(qr_uri).save(_address_qr)
    qrcode = b64encode(_address_qr.getvalue()).decode()

    capture_event(current_user.id, 'load_dashboard')

    _show_secrets = request.args.get('show_secrets', 'default')
    _token = request.args.get('token', 'default')

    if request.args.get('show_secrets', 'no') == 'true' and _token and \
            validate_token(_token, 60) == current_user.email:
        seed = wallet.seed()
        spend_key = wallet.spend_key()
        view_key = wallet.view_key()

        return render_template(
            'wallet/dashboard.html',
            transfers=all_transfers,
            sorted_txes=_get_sorted_txs(transfers),
            balances=balances,
            address=address,
            qrcode=qrcode,
            send_form=send_form,
            delete_form=delete_form,
            user=current_user,
            seed=seed,
            spend_key=spend_key,
            view_key=view_key,
        )

    else:
        secrets_form = Secrets()

        return render_template(
            'wallet/dashboard.html',
            transfers=all_transfers,
            sorted_txes=_get_sorted_txs(transfers),
            balances=balances,
            address=address,
            qrcode=qrcode,
            send_form=send_form,
            secrets_form=secrets_form,
            delete_form=delete_form,
            user=current_user,
        )


@wallet_bp.route('/wallet/connect')
@login_required
@check_confirmed
def connect():
    if current_user.wallet_created is False:
        data = {
            'result': 'fail',
            'message': 'Wallet not yet created'
        }
        return jsonify(data)

    if current_user.wallet_connected is False:
        wallet = docker.start_wallet(current_user.id)
        port = docker.get_port(wallet)
        current_user.wallet_connected = docker.container_exists(wallet)
        current_user.wallet_port = port
        current_user.wallet_container = wallet
        current_user.wallet_start = datetime.utcnow()
        db.session.commit()
        capture_event(current_user.id, 'start_wallet')
        data = {
            'result': 'success',
            'message': 'Wallet has been connected'
        }
    else:
        data = {
            'result': 'fail',
            'message': 'Wallet is already connected'
        }

    return jsonify(data)


@wallet_bp.route('/wallet/create')
@login_required
@check_confirmed
def create():
    if current_user.wallet_created is False:
        c = docker.create_wallet(current_user.id)
        cache.store_data(f'init_wallet_{current_user.id}', 30, c)
        capture_event(current_user.id, 'create_wallet')
        current_user.wallet_created = True
        db.session.commit()
        return redirect(url_for('wallet.loading'))
    else:
        return redirect(url_for('wallet.dashboard'))


@wallet_bp.route('/wallet/status')
@login_required
@check_confirmed
def status():
    user_vol = docker.get_user_volume(current_user.id)
    create_container = cache.get_data(f'init_wallet_{current_user.id}')
    data = {
        'created': current_user.wallet_created,
        'connected': current_user.wallet_connected,
        'port': current_user.wallet_port,
        'container': current_user.wallet_container,
        'volume': docker.volume_exists(user_vol),
        'initializing': docker.container_exists(create_container)
    }
    return jsonify(data)


@wallet_bp.route('/wallet/secrets', methods=['GET', 'POST'])
@login_required
@check_confirmed
def secrets():
    secrets_form = Secrets()

    if secrets_form.validate_on_submit():
        password_matches = bcrypt.check_password_hash(
            current_user.password,
            secrets_form.password.data
        )

        if not password_matches:
            flash('Invalid password.')
            return redirect(url_for('wallet.dashboard') + '#secrets')

        else:
            _token = generate_token(current_user.email)
            return redirect(url_for('wallet.dashboard') + f'?show_secrets=true&token={_token}'
                            + '#secrets')


@wallet_bp.route('/wallet/send', methods=['GET', 'POST'])
@login_required
@check_confirmed
def send():
    send_form = Send()
    redirect_url = url_for('wallet.dashboard') + '#send'
    wallet = Wallet(
        proto='http',
        host='127.0.0.1',
        port=current_user.wallet_port,
        username=current_user.id,
        password=current_user.wallet_password
    )
    if send_form.validate_on_submit():
        address = str(send_form.address.data)
        payment_id = str(send_form.payment_id.data) or None
        # noinspection PyUnresolvedReferences
        user = User.query.get(current_user.id)

        # Check if Xolentum wallet is available
        if wallet.connected is False:
            flash('Wallet RPC interface is unavailable at this time. Try again later.')
            capture_event(user.id, 'tx_fail_address_invalid')
            return redirect(redirect_url)

        # Validate destination address
        if not wallet.validate_address(address):
            flash('Invalid Xolentum address provided.')
            capture_event(user.id, 'tx_fail_address_invalid')
            return redirect(redirect_url)

        # Check if we're sweeping or not
        if send_form.amount.data == 'all':
            tx = wallet.transfer(address, None, 'sweep_all')

        else:
            # Make sure the amount provided is a number
            # noinspection PyBroadException
            try:
                amount = to_atomic(Decimal(send_form.amount.data))

            except:
                flash('Invalid Xolentum amount specified.')
                capture_event(user.id, 'tx_fail_amount_invalid')
                return redirect(redirect_url)

            # Validate Payment ID
            if payment_id and not len(payment_id) in [16, 32] and not all(c in string.hexdigits for c in payment_id):
                flash('Invalid payment ID specified.')
                return redirect(redirect_url)

            # Send transfer
            tx = wallet.transfer(address, amount, payment_id=payment_id)

        # Inform user of result and redirect
        if 'message' in tx:
            msg = tx['message'].capitalize()
            msg_lower = tx['message'].replace(' ', '_').lower()
            flash(f'There was a problem sending the transaction: {msg}')
            capture_event(user.id, f'tx_fail_{msg_lower}')
        else:
            flash('Successfully sent transfer.')
            capture_event(user.id, 'tx_success')

        return redirect(redirect_url)
    else:
        for field, errors in send_form.errors.items():
            flash(f'{send_form[field].label}: {", ".join(errors)}')
        return redirect(redirect_url)


def _get_sorted_txs(_txs):
    total = 0
    txs = {}
    sorted_txs = {}
    for tx_type in _txs:
        for t in _txs[tx_type]:
            txs[t['txid']] = {
                'type': tx_type,
                'amount': t['amount'],
                'timestamp': t['timestamp'],
                'fee': t['fee']
            }

    for i in sorted(txs.items(), key=lambda x: x[1]['timestamp']):
        if i[1]['type'] == 'in':
            total += i[1]['amount']
        elif i[1]['type'] == 'out':
            total -= i[1]['amount']
            total -= i[1]['fee']
        sorted_txs[i[0]] = {
            'type': i[1]['type'],
            'amount': i[1]['amount'],
            'timestamp': i[1]['timestamp'],
            'total': total
        }
    return sorted_txs
