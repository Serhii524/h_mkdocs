import os
import tempfile
import unittest

from mkdocs import exceptions
from mkdocs.config import base, defaults


class ConfigBaseTests(unittest.TestCase):

    def test_unrecognised_keys(self):

        c = base.Config(schema=defaults.DEFAULT_CONFIG)
        c.load_dict({
            'not_a_valid_config_option': "test"
        })

        failed, warnings = c.validate()

        self.assertEqual(warnings, [
            ('not_a_valid_config_option',
                'Unrecognised configuration name: not_a_valid_config_option')
        ])

    def test_missing_required(self):

        c = base.Config(schema=defaults.DEFAULT_CONFIG)

        failed, warnings = c.validate()

        self.assertEqual(failed, [
            ('site_name', 'Required configuration not provided.')
        ])

        self.assertEqual(warnings, [])

    def test_load_missing_required(self):
        """
        Users can explicitly set the config file using the '--config' option.
        Allows users to specify a config other than the default `mkdocs.yml`.
        """

        config_file = tempfile.NamedTemporaryFile('w', delete=False)
        try:
            config_file.write(
                "site_dir: output\nsite_uri: http://www.mkdocs.org\n")
            config_file.flush()

            with open(config_file.name, 'rb') as config:
                self.assertRaises(exceptions.ConfigurationError,
                                  base.load_config, config_file=config)
            config_file.close()
        finally:
            try:
                os.remove(config_file.name)
            except Exception:
                # This fails on Windows for some reason
                pass
