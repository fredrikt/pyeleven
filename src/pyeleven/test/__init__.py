"""
Testing the PKCS#11 shim layer
"""
from flask import json
from .. import mechanism, intarray2bytes, find_key

__author__ = 'leifj'

import pkg_resources
import unittest
import logging
import os
import traceback
import subprocess
import tempfile
from PyKCS11 import PyKCS11Error
from PyKCS11.LowLevel import CKR_PIN_INCORRECT
from .. import pk11
from unittest import TestCase
from .. import app

def _find_alts(alts):
    for a in alts:
        if os.path.exists(a):
            return a
    return None

P11_MODULE = _find_alts(['/usr/lib/libsofthsm.so', '/usr/lib/softhsm/libsofthsm.so'])
P11_ENGINE = _find_alts(['/usr/lib/engines/engine_pkcs11.so'])
P11_SPY = _find_alts(['/usr/lib/pkcs11/pkcs11-spy.so'])
PKCS11_TOOL = _find_alts(['/usr/bin/pkcs11-tool'])
OPENSC_TOOL = _find_alts(['/usr/bin/opensc-tool'])
SOFTHSM = _find_alts(['/usr/bin/softhsm'])
OPENSSL = _find_alts(['/usr/bin/openssl'])


if OPENSSL is None:
    raise unittest.SkipTest("OpenSSL not installed")

if SOFTHSM is None:
    raise unittest.SkipTest("SoftHSM not installed")

if OPENSC_TOOL is None:
    raise unittest.SkipTest("OpenSC not installed")

if PKCS11_TOOL is None:
    raise unittest.SkipTest("pkcs11-tool not installed")

if P11_ENGINE is None:
    raise unittest.SkipTest("libengine-pkcs11-openssl is not installed")

p11_test_files = []
softhsm_conf = None
server_cert_pem = None
server_cert_der = None
softhsm_db = None


def _tf():
    f = tempfile.NamedTemporaryFile(delete=False)
    p11_test_files.append(f.name)
    return f.name


def _p(args):
    env = {}
    if softhsm_conf is not None:
        env['SOFTHSM_CONF'] = softhsm_conf
        #print "env SOFTHSM_CONF=%s " % softhsm_conf +" ".join(args)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = proc.communicate()
    if err is not None and len(err) > 0:
        logging.error(err)
    if out is not None and len(out) > 0:
        logging.debug(out)
    rv = proc.wait()
    if rv:
        raise RuntimeError("command exited with code != 0: %d" % rv)

@unittest.skipIf(P11_MODULE is None, "SoftHSM PKCS11 module not installed")
def setup():
    logging.debug("Creating test pkcs11 token using softhsm")

    try:
        global softhsm_conf
        softhsm_db = _tf()
        softhsm_conf = _tf()
        logging.debug("Generating softhsm.conf")
        with open(softhsm_conf, "w") as f:
            f.write("#Generated by pyXMLSecurity test\n0:%s\n" % softhsm_db)
        logging.debug("Initializing the token")
        _p([SOFTHSM,
            '--slot', '0',
            '--label', 'test',
            '--init-token',
            '--pin', 'secret1',
            '--so-pin', 'secret2'])
        logging.debug("Generating 1024 bit RSA key in token")
        _p([PKCS11_TOOL,
            '--module', P11_MODULE,
            '-l',
            '-k',
            '--key-type', 'rsa:1024',
            '--slot', '0',
            '--id', 'a1b2',
            '--label', 'test',
            '--pin', 'secret1'])
        _p([PKCS11_TOOL,
            '--module', P11_MODULE,
            '-l',
            '--pin', 'secret1', '-O'])
        global signer_cert_der
        global signer_cert_pem
        signer_cert_pem = _tf()
        openssl_conf = _tf()
        logging.debug("Generating OpenSSL config")
        with open(openssl_conf, "w") as f:
            f.write("""
openssl_conf = openssl_def

[openssl_def]
engines = engine_section

[engine_section]
pkcs11 = pkcs11_section

[pkcs11_section]
engine_id = pkcs11
dynamic_path = %s
MODULE_PATH = %s
PIN = secret1
init = 0

[req]
distinguished_name = req_distinguished_name

[req_distinguished_name]
                """ % (P11_ENGINE, P11_MODULE))

        signer_cert_der = _tf()

        logging.debug("Generating self-signed certificate")
        _p([OPENSSL, 'req',
            '-new',
            '-x509',
            '-subj', "/cn=Test Signer",
            '-engine', 'pkcs11',
            '-config', openssl_conf,
            '-keyform', 'engine',
            '-key', 'a1b2',
            '-passin', 'pass:secret1',
            '-out', signer_cert_pem])

        _p([OPENSSL, 'x509',
            '-inform', 'PEM',
            '-outform', 'DER',
            '-in', signer_cert_pem,
            '-out', signer_cert_der])

        logging.debug("Importing certificate into token")

        _p([PKCS11_TOOL,
            '--module', P11_MODULE,
            '-l',
            '--slot', '0',
            '--id', 'a1b2',
            '--label', 'test',
            '-y', 'cert',
            '-w', signer_cert_der,
            '--pin', 'secret1'])

    except Exception, ex:
        traceback.print_exc()
        logging.warning("PKCS11 tests disabled: unable to initialize test token: %s" % ex)


def teardown(self):
    for o in self.p11_test_files:
        if os.path.exists(o):
            os.unlink(o)
    self.p11_test_files = []


class FlaskTestCase(TestCase):
    def setUp(self):
        os.environ['SOFTHSM_CONF'] = softhsm_conf
        app.config['TESTING'] = True
        app.config['PKCS11MODULE'] = P11_MODULE
        app.config['PKCS11PIN'] = 'secret1'
        self.app = app.test_client()

    def test_info(self):
        rv = self.app.get("/info")
        assert rv.data
        d = json.loads(rv.data)
        assert d is not None
        assert 'library' in d
        assert d['library'] == P11_MODULE


class TestPKCS11(unittest.TestCase):
    def setUp(self):
        datadir = pkg_resources.resource_filename(__name__, 'data')

    def test_open_session(self):
        os.environ['SOFTHSM_CONF'] = softhsm_conf
        with pk11.pkcs11(P11_MODULE, 0, "secret1") as session:
            assert session is not None

    def test_find_key(self):
        os.environ['SOFTHSM_CONF'] = softhsm_conf
        with pk11.pkcs11(P11_MODULE, 0, "secret1") as session:
            print session
            assert session is not None
            key, cert = find_key(session, 'test')
            assert key is not None
            assert cert is not None

    def test_sign(self):
        os.environ['SOFTHSM_CONF'] = softhsm_conf
        with pk11.pkcs11(P11_MODULE, 0, "secret1") as session:
            key, cert = find_key(session, 'test')
            signed = intarray2bytes(session.sign(key, 'test', mechanism('RSAPKCS1')))
            assert signed is not None
            print signed