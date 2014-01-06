import time
import random
import base64
import hashlib
import traceback
import binascii

import tools
import messages_pb2 as proto
import types_pb2 as proto_types
import machine_signing
from mnemonic import Mnemonic
from storage import NotInitializedException
from bip32 import BIP32
import coindef

class PinState(object):
    def __init__(self, layout, storage):
        self.layout = layout
        self.storage = storage
        self.matrix = None

        self.set_main_state()

    def set_main_state(self):
        self.cancel()

    def is_waiting(self):
        return self.func is not None

    def _generate_matrix(self):
        # Generate random order of numbers 1-9
        matrix = range(1, 10)
        random.shuffle(matrix)
        return matrix

    def _decode_from_matrix(self, pin_encoded):
        # Receive pin encoded using a matrix
        # Return original PIN sequence
        pin = ''.join([ str(self.matrix[int(x) - 1]) for x in pin_encoded ])        
        return pin    
        
    def request(self, msg, pass_or_check, func, *args):
        self.pass_or_check = pass_or_check
        self.func = func
        self.args = args
        self.matrix = self._generate_matrix()
        
        if not msg:
            msg = 'Please enter your PIN'
        
        self.layout.show_matrix(self.matrix)
        return proto.PinMatrixRequest(message=msg)

    def request_new(self, func, *args):
        '''Ask user for new PIN'''
        return self.request('Please enter new PIN', True, self._request_second, func, args)
        
    def _request_second(self, pin, func, args):
        '''Ask second time for new PIN to confirm user's entry'''
        return self.request('Enter new PIN again', True, self._request_compare, *[pin, func, args])
        
    def _request_compare(self, pin2, pin1, func, args):
        '''Compare both pins and return if they're the same'''
        if pin2 != pin1:
            raise Exception("Pin is different")
        return func(pin1, *args)
        
    def check(self, pin_encoded):
        try:
            pin = self._decode_from_matrix(pin_encoded)
        except ValueError:
            return proto.Failure(code=proto_types.Failure_SyntaxError, message="Syntax error")
        
        if self.pass_or_check:
            # Pass PIN to method
            func = self.func
            args = self.args[:]
            self.cancel()
            msg = func(pin, *args)
            return msg
        else:
            # Check PIN against device's internal PIN
            if pin == self.storage.get_pin():
                func = self.func
                args = self.args
                self.cancel()
                self.storage.clear_pin_attempt()
                msg = func(*args)

                return msg
            else:
                self.storage.increase_pin_attempt()
                print "Invalid PIN, waiting %s seconds" % self.storage.get_pin_delay()
                time.sleep(self.storage.get_pin_delay())
                self.cancel()
                self.set_main_state()
                return proto.Failure(code=proto_types.Failure_PinInvalid, message="Invalid PIN")

    def cancel(self):
        self.pass_or_check = False
        self.func = None
        self.args = []
        self.matrix = None

class ResetWalletState(object):
    def __init__(self, layout, storage, yesno, pin, main_state_func):
        self.layout = layout
        self.storage = storage
        self.yesno = yesno
        self.pin = pin
        self._set_main_state = main_state_func
        self.set_main_state()
        
    def set_main_state(self):
        self.internal_entropy = None
        self.external_entropy = None
        self.strength = None
        self.passphrase_protection = False
        self.pin_protection = False
        self.language = 'english'
        self.label = ''
    
    def is_waiting(self):
        if self.internal_entropy:
            return True
        return False

    def step1(self, display_random, strength, passphrase_protection, pin_protection, language, label):
        '''This starts resetting workflow by generating internal random
        and asking user to confirm device reset.'''
        
        print "Starting device reset..."
        internal_entropy = tools.get_local_entropy()
        
        msg = ["Reset device?"]
        if display_random:
            msg += ["Random is %s" % binascii.hexlify(internal_entropy)]
        
        def entropy_request():
            '''This is called after user confirmation of the action.
            Internal random is already generated, lets respond to computer with EntropyRequest
            and wait for EntropyAck'''
            self.internal_entropy = internal_entropy
            self.external_entropy = None
            self.strength = strength
            self.passphrase_protection = passphrase_protection
            self.pin_protection = pin_protection
            self.label = label
            
            if language in self.storage.get_languages():
                self.language = language
            else:
                self.language = self.storage.struct.language

            return proto.EntropyRequest()
        
        return self.yesno.request(msg, '', 'Confirm }', '{ Cancel', entropy_request)
    
    def step2(self, external_entropy):
        '''Now the action is confirmed by user and both
        internal and external entropy is generated. Lets ask for PIN
        if the device need to be pin-protected
        '''
        self.external_entropy = external_entropy
        
        if self.pin_protection:
            return self.pin.request_new(self.step3)

        else:
            return self.step3('')
            
    def step3(self, pin):
        '''Display mnemonic and ask user to write it down to piece of paper'''
        if self.pin_protection and not pin:
            raise Exception("Pin need to be provided")
        
        if self.pin_protection == False:
            pin = ''
            
        print "Internal entropy:", binascii.hexlify(self.internal_entropy)
        print "Computer-generated entropy:", binascii.hexlify(self.external_entropy)
        
        entropy = tools.generate_entropy(self.strength, self.internal_entropy, self.external_entropy)
        mnemonic = Mnemonic(self.language).to_mnemonic(entropy)
        
        return self.yesno.request(mnemonic.split(" "), '', 'Done }', '{ Cancel', self.step4, *[pin, mnemonic])

    def step4(self, pin, mnemonic):
        self.storage.reset_seed(mnemonic)
        self.storage.set_language(self.language)
        self.storage.set_label(self.label)
        self.storage.set_pin(pin)
        self.storage.set_protection(self.passphrase_protection)
        
        self._set_main_state() 
        return proto.Success(message='Wallet loaded')
        
class YesNoState(object):
    def __init__(self, layout):
        self.layout = layout

        self.set_main_state()

    def set_main_state(self):
        self.cancel()

    def is_waiting(self):
        # We're waiting for confirmation from computer
        return self.func is not None and self.pending

    def allow(self):
        # Computer confirms that we can accept button press now
        self.pending = False

    def cancel(self):
        self.pending = False
        self.decision = None
        self.func = None
        self.args = []

    def request(self, message, question, yes_text, no_text, func, *args):
        self.layout.show_question(message, question, yes_text, no_text)

        self.func = func
        self.args = args
        self.pending = True  # Waiting for confirmation from computer

        # Tell computer that device is waiting for HW buttons
        return proto.ButtonRequest()

    def store(self, button):
        if not self.func:
            return

        self.decision = button

    def resolve(self):
        if not self.func:
            # We're not waiting for hw buttons
            return

        if self.pending:
            # We still didn't received ButtonAck from computer
            return

        if self.decision is None:
            # We still don't know user's decision (call yesno_store() firstly)
            return

        func = self.func
        args = self.args
        decision = self.decision
        self.func = None
        self.args = []
        self.decision = None

        if decision is True:
            ret = func(*args)
        else:
            self.set_main_state()
            ret = proto.Failure(code=proto_types.Failure_ActionCancelled, message='Action cancelled by user')

        return ret


class StateMachine(object):
    def __init__(self, storage, layout):
        self.storage = storage
        self.layout = layout

        self.yesno = YesNoState(layout)
        self.pin = PinState(layout, storage)
        self.signing = machine_signing.SigningStateMachine(layout, storage)
        self.reset_wallet = ResetWalletState(layout, storage, self.yesno, self.pin, self.set_main_state)

        self.set_main_state()
    
    def protect_load(self, seed, node, pin, passphrase_protection):
        return self.yesno.request(["Load custom seed?"], '', 'Confirm }', '{ Cancel', self.load_wallet, *[seed, node, pin, passphrase_protection])

    def protect_call(self, yesno_message, question, no_text, yes_text, func, *args):
        '''
            yesno_message - display text on the main part of the display
            question - short question in status bar (above buttons)
            no_text - text of the left button
            yes_text - text of the right button
            func - which function to call when user passes the protection
            *args - arguments for func
        '''  
            
        if self.storage.get_pin():
            # Require hw buttons and PIN
            return self.yesno.request(yesno_message, question, yes_text, no_text, self.pin.request,
                                      *['', False, func] + list(args))

        # If confirmed, call final function directly
        return self.yesno.request(yesno_message, question, yes_text, no_text, func, *args)

    def clear_custom_message(self):
        if self.custom_message:
            self.custom_message = False
            self.layout.show_logo(None, self.storage.get_label())

    def press_button(self, button):
        if button and self.custom_message:
            self.clear_custom_message()

        self.yesno.store(button)
        ret = self.yesno.resolve()
        if isinstance(ret, proto.Failure):
            self.set_main_state()
        return ret

    def debug_get_state(self, msg):
        resp = proto.DebugLinkState()
        if msg.pin:
            resp.pin = self.storage.get_pin()
        if msg.matrix and self.pin.is_waiting():
            resp.matrix = ''.join([ str(x) for x in self.pin.matrix ])
        return resp

    def set_main_state(self):
        # Switch device to default state
        self.yesno.set_main_state()
        self.signing.set_main_state()
        self.pin.set_main_state()
        self.reset_wallet.set_main_state()

        # Display is showing custom message which just wait for "Continue" button,
        # but doesn't require any interaction with computer
        self.custom_message = False
    
        if self.storage.is_initialized():
            self.layout.show_logo(None, self.storage.get_label())   
        else:
            self.layout.show_message(
                ["Device hasn't been",
                 "initialized yet.",
                 "Please initialize it",
                 "from desktop client."])
    
    def apply_settings(self, settings):
        message = []
        # FIXME
        raise Exception("Not implemented")
        '''
        if settings.language and settings.language in self.storage.get_languages():
            message.append('Language: %s' % settings.language)
        else:
            settings.language = ''

        if settings.coin_shortcut and settings.coin_shortcut in coindef.types.keys():
            message.append('Coin: %s' % coindef.types[settings.coin_shortcut].coin_name)
        else:
            settings.coin_shortcut = ''

        if settings.label:
            message.append('Label: %s' % settings.label)
        else:
            settings.label = ''
            
        question = 'Apply these settings?'
        func = self._apply_settings
        args = (settings,)

        return self.protect_call(message, question, '{ Cancel', 'Confirm }', func, *args)
        
    def _apply_settings(self, settings):
        if settings.language:
            self.storage.struct.settings.language = settings.language
            
        if settings.coin_shortcut:
            self.storage.struct.settings.coin.CopyFrom(coindef.types[settings.coin_shortcut])

        if settings.label:
            self.storage.struct.settings.label = settings.label

        self.storage.save()
        self.set_main_state()
        return proto.Success(message='Settings updated')
    '''
    
    def load_wallet(self, mnemonic, node, pin, passphrase_protection, language, label):
        # Use mnemonic OR HDNodeType to initialize the device
        # If both are provided, mnemonic has higher priority

        if mnemonic:
            self.storage.load_from_mnemonic(mnemonic)
        else:
            self.storage.load_from_node(node)
        
        self.storage.set_language(language)
        self.storage.set_label(label)
        self.storage.set_pin(pin)
        self.storage.set_protection(passphrase_protection)
        self.set_main_state()
        return proto.Success(message='Wallet loaded')

    def magic(self, message):
        magic = "\x18Bitcoin Signed Message:\n" + chr(len(message)) + message
        return magic

    def _sign_message(self, bip32, addr_n, message):
        signer = bip32.get_signer(addr_n)
        address = bip32.get_address(addr_n, self.storage.get_address_type())

        magic = self.magic(message)
        signature = signer.sign_deterministic(hashlib.sha256(magic).digest(), hashfunc=hashlib.sha256)

        for i in range(4):
            sig = base64.b64encode(chr(27 + i + 4) + signature)
            print sig
            if self._verify_message(address, sig, message):
                return proto.MessageSignature(address=address, signature=sig)

        return proto.Failure(code=proto_types.Failure_InvalidSignature, message="Cannot sign message")

    def _verify_message(self, address, signature, message):
        """ See http://www.secg.org/download/aid-780/sec1-v2.pdf for the math """
        from ecdsa import numbertheory, ellipticcurve, util
        import ecdsa
        import msqr
        import binascii
        curve = ecdsa.curves.SECP256k1.curve  # curve_secp256k1
        G = ecdsa.curves.SECP256k1.generator
        order = G.order()
        # extract r,s from signature
        sig = base64.b64decode(signature)
        if len(sig) != 65: raise BaseException("Wrong encoding")
        r, s = util.sigdecode_string(sig[1:], order)
        nV = ord(sig[0])
        if nV < 27 or nV >= 35:
            raise BaseException("Bad encoding")
        if nV >= 31:
            compressed = True
            nV -= 4
        else:
            compressed = False

        recid = nV - 27
        # 1.1
        x = r + (recid / 2) * order
        # 1.3
        alpha = (x * x * x + curve.a() * x + curve.b()) % curve.p()
        beta = msqr.modular_sqrt(alpha, curve.p())
        y = beta if (beta - recid) % 2 == 0 else curve.p() - beta
        # 1.4 the constructor checks that nR is at infinity
        R = ellipticcurve.Point(curve, x, y, order)
        # 1.5 compute e from message:
        h = hashlib.sha256(hashlib.sha256(self.magic(message)).digest()).digest()
        e = util.string_to_number(h)
        minus_e = -e % order
        # 1.6 compute Q = r^-1 (sR - eG)
        inv_r = numbertheory.inverse_mod(r, order)
        Q = inv_r * (s * R + minus_e * G)
        public_key = ecdsa.VerifyingKey.from_public_point(Q, curve=ecdsa.curves.SECP256k1)
        # check that Q is the public key
        public_key.verify_digest(sig[1:], h, sigdecode=ecdsa.util.sigdecode_string)
        addr = tools.public_key_to_bc_address('\x04' + public_key.to_string(), self.storage.get_address_type(), compress=compressed)

        return address == addr

    def _get_entropy(self, size):
        random.seed()
        m = proto.Entropy()
        d = ''
        while len(d) < size:
            d += tools.generate_seed(tools.STRENGTH_HIGH, '')

        m.entropy = d[:size]
        self.set_main_state()
        return m

    def _process_message(self, msg):
        if isinstance(msg, proto.Initialize):
            self.set_main_state()
            return self.storage.get_features()

        if self.pin.is_waiting():
            '''PIN response is expected'''
            if isinstance(msg, proto.PinMatrixAck):
                return self.pin.check(msg.pin)

            if isinstance(msg, proto.Cancel):
                self.pin.cancel()
                return proto.Failure(code=proto_types.Failure_PinCancelled, message="PIN request cancelled")

            self.set_main_state()
            return proto.Failure(code=proto_types.Failure_PinExpected, message='PIN expected')

        if self.yesno.is_waiting():
            '''Button confirmation is expected'''
            if isinstance(msg, proto.ButtonAck):
                self.yesno.allow()
                return self.yesno.resolve()  # Process if button has been already pressed

            if isinstance(msg, proto.Cancel):
                self.set_main_state()
                return proto.Success(message="Button confirmation cancelled")

            self.set_main_state()
            return proto.Failure(code=proto_types.Failure_ButtonExpected, message='Button confirmation expected')

        if self.reset_wallet.is_waiting():
            if isinstance(msg, proto.EntropyAck):
                return self.reset_wallet.step2(msg.entropy)

            self.set_main_state()
            return proto.Failure(code=proto_types.Failure_UnexpectedMessage, message='EntropyAck expected')

        if isinstance(msg, proto.Ping):
            return proto.Success(message=msg.message)

        if isinstance(msg, proto.FirmwareUpload):
            if msg.payload[:4] != 'TRZR':
                return proto.Failure(code=proto_types.Failure_SyntaxError, message='Firmware header expected')
            return proto.Success(message='%d bytes of firmware succesfully uploaded' % len(msg.payload))

        if isinstance(msg, proto.GetEntropy):
            return self.protect_call(["Send %d bytes" % msg.size, "of entropy", "to computer?"], '',
                                     '{ Cancel', 'Confirm }', self._get_entropy, msg.size)

        if isinstance(msg, proto.GetPublicKey):
            node = BIP32(self.storage.get_node()).get_public_node(list(msg.address_n))
            return proto.PublicKey(node=node)

        if isinstance(msg, proto.GetAddress):
            address = BIP32(self.storage.get_node()).get_address(list(msg.address_n), self.storage.get_address_type())
            self.layout.show_receiving_address(address)
            self.custom_message = True  # Yes button will redraw screen
            return proto.Address(address=address)

        if isinstance(msg, proto.ApplySettings):
            return self.apply_settings(msg)

        if isinstance(msg, proto.LoadDevice):
            return self.protect_load(msg.mnemonic, msg.node, msg.pin, msg.passphrase_protection)

        if isinstance(msg, proto.ResetDevice):
            return self.reset_wallet.step1(msg.display_random, msg.strength, msg.passphrase_protection, msg.pin_protection, msg.language, msg.label)
        
        if isinstance(msg, proto.SignMessage):
            return self._sign_message(BIP32(self.storage.get_node()), list(msg.address_n), msg.message)
            # return self.protect_call(["Sign message?", msg.message], '', '{ Cancel', 'Confirm }', self._sign_message, BIP32(self.storage.get_node()), msg.address_n, msg.message)

        if isinstance(msg, proto.VerifyMessage):
            if self._verify_message(msg.address, msg.signature, msg.message):
                return proto.Success()
            else:
                return proto.Failure(code=proto_types.Failure_InvalidSignature, message="Invalid signature")

        if isinstance(msg, (proto.SignTx, proto.TxInput, proto.TxOutput)):
            return self.signing.process_message(msg)

        self.set_main_state()
        return proto.Failure(code=proto_types.Failure_UnexpectedMessage, message="Unexpected message")

    def _process_debug_message(self, msg):
        if isinstance(msg, proto.DebugLinkGetState):
            # Report device state
            return self.debug_get_state(msg)

        if isinstance(msg, proto.DebugLinkStop):
            import sys
            sys.exit()

        self.set_main_state()
        return proto.Failure(code=proto_types.Failure_UnexpectedMessage, message="Unexpected message")

    def process_message(self, msg):
        # Any exception thrown during message processing
        # will result in Failure message instead of application crash
        try:
            ret = self._process_message(msg)
            if isinstance(ret, proto.Failure):
                self.set_main_state()
            return ret
        except Exception as exc:
            traceback.print_exc()
            self.set_main_state()
            return proto.Failure(message=str(exc))

    def process_debug_message(self, msg):
        # Process messages handled by debugging connection
        try:
            return self._process_debug_message(msg)
        except Exception as exc:
            traceback.print_exc()
            self.set_main_state()
            return proto.Failure(message=str(exc))
