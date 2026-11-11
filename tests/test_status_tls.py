import os
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tauceti_fleet import ci_overview


class StatusTLS(unittest.TestCase):
    def context(self, *, cafile=None, environment=None, bundles=()):
        context = Mock()
        with patch.dict(os.environ, environment or {}, clear=True), \
                patch.object(ssl, "create_default_context", return_value=context), \
                patch.object(ssl, "get_default_verify_paths", return_value=SimpleNamespace(cafile=cafile)), \
                patch.object(ci_overview.Path, "is_file", lambda p: str(p) in bundles):
            self.assertIs(ci_overview.status_ssl_context(), context)
        return context

    def test_standalone_python_loads_the_installed_system_bundle(self):
        bundle = "/etc/ssl/certs/ca-certificates.crt"
        self.context(bundles=(bundle,)).load_verify_locations.assert_called_once_with(cafile=bundle)

    def test_nix_bundle_name_is_also_supported(self):
        bundle = "/etc/ssl/certs/ca-bundle.crt"
        self.context(bundles=(bundle,)).load_verify_locations.assert_called_once_with(cafile=bundle)

    def test_native_default_bundle_is_preserved(self):
        self.context(cafile="/native/cert.pem").load_verify_locations.assert_not_called()

    def test_explicit_trust_configuration_is_never_replaced(self):
        for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            for value in ("/custom/trust", ""):
                with self.subTest(key=key, value=value):
                    self.context(environment={key: value}).load_verify_locations.assert_not_called()

    def test_absent_system_bundle_does_not_disable_verification(self):
        self.context().load_verify_locations.assert_not_called()
        context = ci_overview.status_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_status_requests_install_the_verified_context_and_keep_no_redirect(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"fresh":true}'
        opener = Mock()
        opener.open.return_value = response
        context = Mock()
        with patch.object(ci_overview, "status_ssl_context", return_value=context), \
                patch.object(ci_overview.urllib.request, "HTTPSHandler") as handler, \
                patch.object(ci_overview.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(ci_overview.fetch("https://example.test/status", timeout=3), {"fresh": True})
        handler.assert_called_once_with(context=context)
        self.assertIsInstance(build.call_args.args[0], ci_overview.NoRedirect)
        self.assertIs(build.call_args.args[1], handler.return_value)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
