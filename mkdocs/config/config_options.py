from __future__ import annotations

import functools
import ipaddress
import os
import string
import sys
import traceback
import typing as t
import warnings
from collections import UserString
from typing import Collection, Dict, Generic, List, NamedTuple, Tuple, TypeVar, Union, overload
from urllib.parse import quote as urlquote
from urllib.parse import urlsplit, urlunsplit

import markdown

from mkdocs import plugins, theme, utils
from mkdocs.config.base import (
    BaseConfigOption,
    Config,
    LegacyConfig,
    PlainConfigSchemaItem,
    ValidationError,
)
from mkdocs.exceptions import ConfigurationError

T = TypeVar('T')
SomeConfig = TypeVar('SomeConfig', bound=Config)


class SubConfig(Generic[SomeConfig], BaseConfigOption[SomeConfig]):
    """
    Subconfig Config Option

    New: If targeting MkDocs 1.4+, please pass a subclass of Config to the
    constructor, instead of the old style of a sequence of ConfigOption instances.
    Validation is then enabled by default.

    A set of `config_options` grouped under a single config option.
    By default, validation errors and warnings resulting from validating
    `config_options` are ignored (`validate=False`). Users should typically
    enable validation with `validate=True`.
    """

    @overload
    def __init__(
        self: SubConfig[SomeConfig], config_class: t.Type[SomeConfig], *, validate: bool = True
    ):
        """Create a sub-config in a type-safe way, using fields defined in a Config subclass."""

    @overload
    def __init__(
        self: SubConfig[LegacyConfig],
        *config_options: PlainConfigSchemaItem,
        validate: bool = False,
    ):
        """Create an untyped sub-config, using directly passed fields."""

    def __init__(self, *config_options, validate=None):
        super().__init__()
        self.default = {}
        if (
            len(config_options) == 1
            and isinstance(config_options[0], type)
            and issubclass(config_options[0], Config)
        ):
            if validate is None:
                validate = True
            (self._make_config,) = config_options
        else:
            self._make_config = functools.partial(LegacyConfig, config_options)
        self._do_validation = bool(validate)

    def run_validation(self, value):
        config = self._make_config()
        try:
            config.load_dict(value)
            failed, warnings = config.validate()
        except ConfigurationError as e:
            raise ValidationError(str(e))

        if self._do_validation:
            # Capture errors and warnings
            self.warnings = warnings
            if failed:
                # Get the first failing one
                key, err = failed[0]
                raise ValidationError(f"Sub-option {key!r} configuration error: {err}")

        return config


class OptionallyRequired(Generic[T], BaseConfigOption[T]):
    """
    A subclass of BaseConfigOption that adds support for default values and
    required values. It is a base class for config options.
    """

    @overload
    def __init__(self, default=None):
        ...

    @overload
    def __init__(self, default=None, *, required: bool):
        ...

    def __init__(self, default=None, required=None):
        super().__init__()
        self.default = default
        self._legacy_required = required
        self.required = bool(required)

    def validate(self, value):
        """
        Perform some initial validation.

        If the option is empty (None) and isn't required, leave it as such. If
        it is empty but has a default, use that. Finally, call the
        run_validation method on the subclass unless.
        """
        if value is None:
            if self.default is not None:
                value = self.default
            elif not self.required:
                return None
            elif self.required:
                raise ValidationError("Required configuration not provided.")

        return self.run_validation(value)


class ListOfItems(Generic[T], BaseConfigOption[List[T]]):
    """
    Validates a homogeneous list of items.

    E.g. for `config_options.ListOfItems(config_options.Type(int))` a valid item is `[1, 2, 3]`.
    """

    required: Union[bool, None] = None  # Only for subclasses to set.

    def __init__(self, option_type: BaseConfigOption[T], default=None):
        super().__init__()
        self.default = default
        self.option_type = option_type
        self.option_type.warnings = self.warnings

    def __repr__(self):
        return f'{type(self).__name__}: {self.option_type}'

    def pre_validation(self, config, key_name):
        self._config = config
        self._key_name = key_name

    def run_validation(self, value):
        if value is None:
            if self.required or self.default is None:
                raise ValidationError("Required configuration not provided.")
            value = self.default
        if not isinstance(value, list):
            raise ValidationError(f'Expected a list of items, but a {type(value)} was given.')
        if not value:  # Optimization for empty list
            return value

        fake_config = Config(())
        try:
            fake_config.config_file_path = self._config.config_file_path
        except AttributeError:
            pass

        # Emulate a config-like environment for pre_validation and post_validation.
        parent_key_name = getattr(self, '_key_name', '')
        fake_keys = [f'{parent_key_name}[{i}]' for i in range(len(value))]
        fake_config.data = dict(zip(fake_keys, value))

        for key_name in fake_config:
            self.option_type.pre_validation(fake_config, key_name)
        for key_name in fake_config:
            # Specifically not running `validate` to avoid the OptionallyRequired effect.
            fake_config[key_name] = self.option_type.run_validation(fake_config[key_name])
        for key_name in fake_config:
            self.option_type.post_validation(fake_config, key_name)

        return [fake_config[k] for k in fake_keys]


class ConfigItems(ListOfItems[LegacyConfig]):
    """
    Deprecated: Use `ListOfItems(SubConfig(...))` instead of `ConfigItems(...)`.

    Validates a list of mappings that all must match the same set of
    options.
    """

    @overload
    def __init__(self, *config_options: PlainConfigSchemaItem):
        ...

    @overload
    def __init__(self, *config_options: PlainConfigSchemaItem, required: bool):
        ...

    def __init__(self, *config_options: PlainConfigSchemaItem, required=None):
        super().__init__(SubConfig(*config_options), default=[])
        self._legacy_required = required
        self.required = bool(required)


class Type(Generic[T], OptionallyRequired[T]):
    """
    Type Config Option

    Validate the type of a config option against a given Python type.
    """

    @overload
    def __init__(self, type_: t.Type[T], length: t.Optional[int] = None, **kwargs):
        ...

    @overload
    def __init__(self, type_: Tuple[t.Type[T], ...], length: t.Optional[int] = None, **kwargs):
        ...

    def __init__(self, type_, length=None, **kwargs):
        super().__init__(**kwargs)
        self._type = type_
        self.length = length

    def run_validation(self, value):
        if not isinstance(value, self._type):
            msg = f"Expected type: {self._type} but received: {type(value)}"
        elif self.length is not None and len(value) != self.length:
            msg = (
                f"Expected type: {self._type} with length {self.length}"
                f" but received: {value!r} with length {len(value)}"
            )
        else:
            return value

        raise ValidationError(msg)


class Choice(Generic[T], OptionallyRequired[T]):
    """
    Choice Config Option

    Validate the config option against a strict set of values.
    """

    def __init__(self, choices: Collection[T], default: t.Optional[T] = None, **kwargs):
        super().__init__(default=default, **kwargs)
        try:
            length = len(choices)
        except TypeError:
            length = 0

        if not length or isinstance(choices, str):
            raise ValueError(f'Expected iterable of choices, got {choices}')
        if default is not None and default not in choices:
            raise ValueError(f'{default!r} is not one of {choices!r}')

        self.choices = choices

    def run_validation(self, value):
        if value not in self.choices:
            raise ValidationError(f"Expected one of: {self.choices} but received: {value!r}")
        return value


class Deprecated(BaseConfigOption):
    """
    Deprecated Config Option

    Raises a warning as the option is deprecated. Uses `message` for the
    warning. If `move_to` is set to the name of a new config option, the value
    is moved to the new option on pre_validation. If `option_type` is set to a
    ConfigOption instance, then the value is validated against that type.
    """

    def __init__(
        self,
        moved_to: t.Optional[str] = None,
        message: t.Optional[str] = None,
        removed: bool = False,
        option_type: t.Optional[BaseConfigOption] = None,
    ):
        super().__init__()
        self.default = None
        self.moved_to = moved_to
        if not message:
            if removed:
                message = "The configuration option '{}' was removed from MkDocs."
            else:
                message = (
                    "The configuration option '{}' has been deprecated and "
                    "will be removed in a future release of MkDocs."
                )
            if moved_to:
                message += f" Use '{moved_to}' instead."

        self.message = message
        self.removed = removed
        self.option = option_type or BaseConfigOption()

        self.warnings = self.option.warnings

    def pre_validation(self, config, key_name):
        self.option.pre_validation(config, key_name)

        if config.get(key_name) is not None:
            if self.removed:
                raise ValidationError(self.message.format(key_name))
            self.warnings.append(self.message.format(key_name))

            if self.moved_to is not None:
                *parent_keys, target_key = self.moved_to.split('.')
                target = config

                for key in parent_keys:
                    if target.get(key) is None:
                        target[key] = {}
                    target = target[key]

                    if not isinstance(target, dict):
                        # We can't move it for the user
                        return

                target[target_key] = config.pop(key_name)

    def validate(self, value):
        return self.option.validate(value)

    def post_validation(self, config, key_name):
        self.option.post_validation(config, key_name)

    def reset_warnings(self):
        self.option.reset_warnings()
        self.warnings = self.option.warnings


class _IpAddressValue(NamedTuple):
    host: str
    port: int

    def __str__(self):
        return f'{self.host}:{self.port}'


class IpAddress(OptionallyRequired[_IpAddressValue]):
    """
    IpAddress Config Option

    Validate that an IP address is in an appropriate format
    """

    def run_validation(self, value):
        try:
            host, port = value.rsplit(':', 1)
        except Exception:
            raise ValidationError("Must be a string of format 'IP:PORT'")

        if host != 'localhost':
            if host.startswith('[') and host.endswith(']'):
                host = host[1:-1]
            try:
                # Validate and normalize IP Address
                host = str(ipaddress.ip_address(host))
            except ValueError as e:
                raise ValidationError(e)

        try:
            port = int(port)
        except Exception:
            raise ValidationError(f"'{port}' is not a valid port")

        return _IpAddressValue(host, port)

    def post_validation(self, config, key_name):
        host = config[key_name].host
        if key_name == 'dev_addr' and host in ['0.0.0.0', '::']:
            self.warnings.append(
                f"The use of the IP address '{host}' suggests a production environment "
                "or the use of a proxy to connect to the MkDocs server. However, "
                "the MkDocs' server is intended for local development purposes only. "
                "Please use a third party production-ready server instead."
            )


class URL(OptionallyRequired[str]):
    """
    URL Config Option

    Validate a URL by requiring a scheme is present.
    """

    @overload
    def __init__(self, default=None, *, is_dir: bool = False):
        ...

    @overload
    def __init__(self, default=None, *, required: bool, is_dir: bool = False):
        ...

    def __init__(self, default=None, required=None, is_dir: bool = False):
        self.is_dir = is_dir
        super().__init__(default, required=required)

    def run_validation(self, value):
        if value == '':
            return value

        try:
            parsed_url = urlsplit(value)
        except (AttributeError, TypeError):
            raise ValidationError("Unable to parse the URL.")

        if parsed_url.scheme and parsed_url.netloc:
            if self.is_dir and not parsed_url.path.endswith('/'):
                parsed_url = parsed_url._replace(path=f'{parsed_url.path}/')
            return urlunsplit(parsed_url)

        raise ValidationError("The URL isn't valid, it should include the http:// (scheme)")


class Optional(Generic[T], BaseConfigOption[Union[T, None]]):
    """Wraps a field and makes a None value possible for it when no value is set.

    E.g. `my_field = config_options.Optional(config_options.Type(str))`
    """

    def __init__(self, config_option: BaseConfigOption[T]):
        super().__init__()
        self.option = config_option
        self.warnings = config_option.warnings

    def __getattr__(self, key):
        if key in ('option', 'warnings'):
            raise AttributeError
        return getattr(self.option, key)

    def pre_validation(self, config, key_name):
        return self.option.pre_validation(config, key_name)

    def run_validation(self, value):
        if value is None:
            return self.default
        return self.option.validate(value)

    def post_validation(self, config, key_name):
        result = self.option.post_validation(config, key_name)
        self.warnings = self.option.warnings
        return result

    def reset_warnings(self):
        self.option.reset_warnings()
        self.warnings = self.option.warnings


class RepoURL(URL):
    def __init__(self, *args, **kwargs):
        warnings.warn(
            "RepoURL is no longer used in MkDocs and will be removed.", DeprecationWarning
        )
        super().__init__(*args, **kwargs)

    def post_validation(self, config, key_name):
        repo_host = urlsplit(config['repo_url']).netloc.lower()
        edit_uri = config.get('edit_uri')

        # derive repo_name from repo_url if unset
        if config['repo_url'] is not None and config.get('repo_name') is None:
            if repo_host == 'github.com':
                config['repo_name'] = 'GitHub'
            elif repo_host == 'bitbucket.org':
                config['repo_name'] = 'Bitbucket'
            elif repo_host == 'gitlab.com':
                config['repo_name'] = 'GitLab'
            else:
                config['repo_name'] = repo_host.split('.')[0].title()

        # derive edit_uri from repo_name if unset
        if config['repo_url'] is not None and edit_uri is None:
            if repo_host == 'github.com' or repo_host == 'gitlab.com':
                edit_uri = 'edit/master/docs/'
            elif repo_host == 'bitbucket.org':
                edit_uri = 'src/default/docs/'
            else:
                edit_uri = ''

        # ensure a well-formed edit_uri
        if edit_uri and not edit_uri.endswith('/'):
            edit_uri += '/'

        config['edit_uri'] = edit_uri


class EditURI(Type[str]):
    def __init__(self, repo_url_key: str):
        super().__init__(str)
        self.repo_url_key = repo_url_key

    def post_validation(self, config, key_name):
        edit_uri = config.get(key_name)
        repo_url = config.get(self.repo_url_key)

        if edit_uri is None and repo_url is not None:
            repo_host = urlsplit(repo_url).netloc.lower()
            if repo_host == 'github.com' or repo_host == 'gitlab.com':
                edit_uri = 'edit/master/docs/'
            elif repo_host == 'bitbucket.org':
                edit_uri = 'src/default/docs/'

        # ensure a well-formed edit_uri
        if edit_uri and not edit_uri.endswith('/'):
            edit_uri += '/'

        config[key_name] = edit_uri


class EditURITemplate(BaseConfigOption[str]):
    class Formatter(string.Formatter):
        def convert_field(self, value, conversion):
            if conversion == 'q':
                return urlquote(value, safe='')
            return super().convert_field(value, conversion)

    class Template(UserString):
        def __init__(self, formatter, data):
            super().__init__(data)
            self.formatter = formatter
            try:
                self.format('', '')
            except KeyError as e:
                raise ValueError(f"Unknown template substitute: {e}")

        def format(self, path, path_noext):
            return self.formatter.format(self.data, path=path, path_noext=path_noext)

    def __init__(self, edit_uri_key=None):
        super().__init__()
        self.edit_uri_key = edit_uri_key

    def run_validation(self, value):
        try:
            return self.Template(self.Formatter(), value)
        except Exception as e:
            raise ValidationError(e)

    def post_validation(self, config, key_name):
        if self.edit_uri_key and config.get(key_name) and config.get(self.edit_uri_key):
            self.warnings.append(
                f"The option '{self.edit_uri_key}' has no effect when '{key_name}' is set."
            )


class RepoName(Type[str]):
    def __init__(self, repo_url_key: str):
        super().__init__(str)
        self.repo_url_key = repo_url_key

    def post_validation(self, config, key_name):
        repo_name = config.get(key_name)
        repo_url = config.get(self.repo_url_key)

        # derive repo_name from repo_url if unset
        if repo_url is not None and repo_name is None:
            repo_host = urlsplit(config['repo_url']).netloc.lower()
            if repo_host == 'github.com':
                repo_name = 'GitHub'
            elif repo_host == 'bitbucket.org':
                repo_name = 'Bitbucket'
            elif repo_host == 'gitlab.com':
                repo_name = 'GitLab'
            else:
                repo_name = repo_host.split('.')[0].title()
            config[key_name] = repo_name


class FilesystemObject(Type[str]):
    """
    Base class for options that point to filesystem objects.
    """

    existence_test = staticmethod(os.path.exists)
    name = 'file or directory'

    def __init__(self, exists: bool = False, **kwargs):
        super().__init__(type_=str, **kwargs)
        self.exists = exists
        self.config_dir = None

    def pre_validation(self, config, key_name):
        self.config_dir = (
            os.path.dirname(config.config_file_path) if config.config_file_path else None
        )

    def run_validation(self, value):
        value = super().run_validation(value)
        if self.config_dir and not os.path.isabs(value):
            value = os.path.join(self.config_dir, value)
        if self.exists and not self.existence_test(value):
            raise ValidationError(f"The path '{value}' isn't an existing {self.name}.")
        return os.path.abspath(value)


class Dir(FilesystemObject):
    """
    Dir Config Option

    Validate a path to a directory, optionally verifying that it exists.
    """

    existence_test = staticmethod(os.path.isdir)
    name = 'directory'


class DocsDir(Dir):
    def post_validation(self, config, key_name):
        if config.config_file_path is None:
            return

        # Validate that the dir is not the parent dir of the config file.
        if os.path.dirname(config.config_file_path) == config[key_name]:
            raise ValidationError(
                f"The '{key_name}' should not be the parent directory of the"
                f" config file. Use a child directory instead so that the"
                f" '{key_name}' is a sibling of the config file."
            )


class File(FilesystemObject):
    """
    File Config Option

    Validate a path to a file, optionally verifying that it exists.
    """

    existence_test = staticmethod(os.path.isfile)
    name = 'file'


class ListOfPaths(ListOfItems[str]):
    """
    List of Paths Config Option

    A list of file system paths. Raises an error if one of the paths does not exist.

    For greater flexibility, prefer ListOfItems, e.g. to require files specifically:

        config_options.ListOfItems(config_options.File(exists=True))
    """

    @overload
    def __init__(self, default=[]):
        ...

    @overload
    def __init__(self, default=[], *, required: bool):
        ...

    def __init__(self, default=[], required=None):
        super().__init__(FilesystemObject(exists=True), default)
        self.required = required


class SiteDir(Dir):
    """
    SiteDir Config Option

    Validates the site_dir and docs_dir directories do not contain each other.
    """

    def post_validation(self, config, key_name):
        super().post_validation(config, key_name)
        docs_dir = config['docs_dir']
        site_dir = config['site_dir']

        # Validate that the docs_dir and site_dir don't contain the
        # other as this will lead to copying back and forth on each
        # and eventually make a deep nested mess.
        if (docs_dir + os.sep).startswith(site_dir.rstrip(os.sep) + os.sep):
            raise ValidationError(
                f"The 'docs_dir' should not be within the 'site_dir' as this "
                f"can mean the source files are overwritten by the output or "
                f"it will be deleted if --clean is passed to mkdocs build. "
                f"(site_dir: '{site_dir}', docs_dir: '{docs_dir}')"
            )
        elif (site_dir + os.sep).startswith(docs_dir.rstrip(os.sep) + os.sep):
            raise ValidationError(
                f"The 'site_dir' should not be within the 'docs_dir' as this "
                f"leads to the build directory being copied into itself and "
                f"duplicate nested files in the 'site_dir'. "
                f"(site_dir: '{site_dir}', docs_dir: '{docs_dir}')"
            )


class Theme(BaseConfigOption[theme.Theme]):
    """
    Theme Config Option

    Validate that the theme exists and build Theme instance.
    """

    def __init__(self, default=None):
        super().__init__()
        self.default = default

    def run_validation(self, value):
        if value is None and self.default is not None:
            value = {'name': self.default}

        if isinstance(value, str):
            value = {'name': value}

        themes = utils.get_theme_names()

        if isinstance(value, dict):
            if 'name' in value:
                if value['name'] is None or value['name'] in themes:
                    return value

                raise ValidationError(
                    f"Unrecognised theme name: '{value['name']}'. "
                    f"The available installed themes are: {', '.join(themes)}"
                )

            raise ValidationError("No theme name set.")

        raise ValidationError(f'Invalid type {type(value)}. Expected a string or key/value pairs.')

    def post_validation(self, config, key_name):
        theme_config = config[key_name]

        if not theme_config['name'] and 'custom_dir' not in theme_config:
            raise ValidationError(
                f"At least one of '{key_name}.name' or '{key_name}.custom_dir' must be defined."
            )

        # Ensure custom_dir is an absolute path
        if 'custom_dir' in theme_config and not os.path.isabs(theme_config['custom_dir']):
            config_dir = os.path.dirname(config.config_file_path)
            theme_config['custom_dir'] = os.path.join(config_dir, theme_config['custom_dir'])

        if 'custom_dir' in theme_config and not os.path.isdir(theme_config['custom_dir']):
            raise ValidationError(
                "The path set in {name}.custom_dir ('{path}') does not exist.".format(
                    path=theme_config['custom_dir'], name=key_name
                )
            )

        if 'locale' in theme_config and not isinstance(theme_config['locale'], str):
            raise ValidationError(f"'{key_name}.locale' must be a string.")

        config[key_name] = theme.Theme(**theme_config)


class Nav(OptionallyRequired):
    """
    Nav Config Option

    Validate the Nav config.
    """

    def run_validation(self, value, *, top=True):
        if isinstance(value, list):
            for subitem in value:
                self._validate_nav_item(subitem)
            if top and not value:
                value = None
        elif isinstance(value, dict) and value and not top:
            # TODO: this should be an error.
            self.warnings.append(f"Expected nav to be a list, got {self._repr_item(value)}")
            for subitem in value.values():
                self.run_validation(subitem, top=False)
        elif isinstance(value, str) and not top:
            pass
        else:
            raise ValidationError(f"Expected nav to be a list, got {self._repr_item(value)}")
        return value

    def _validate_nav_item(self, value):
        if isinstance(value, str):
            pass
        elif isinstance(value, dict):
            if len(value) != 1:
                raise ValidationError(
                    f"Expected nav item to be a dict of size 1, got {self._repr_item(value)}"
                )
            for subnav in value.values():
                self.run_validation(subnav, top=False)
        else:
            raise ValidationError(
                f"Expected nav item to be a string or dict, got {self._repr_item(value)}"
            )

    @classmethod
    def _repr_item(cls, value):
        if isinstance(value, dict) and value:
            return f"dict with keys {tuple(value.keys())}"
        elif isinstance(value, (str, type(None))):
            return repr(value)
        else:
            return f"a {type(value).__name__}: {value!r}"


class Private(BaseConfigOption):
    """
    Private Config Option

    A config option only for internal use. Raises an error if set by the user.
    """

    def run_validation(self, value):
        if value is not None:
            raise ValidationError('For internal use only.')


class MarkdownExtensions(OptionallyRequired[List[str]]):
    """
    Markdown Extensions Config Option

    A list or dict of extensions. Each list item may contain either a string or a one item dict.
    A string must be a valid Markdown extension name with no config options defined. The key of
    a dict item must be a valid Markdown extension name and the value must be a dict of config
    options for that extension. Extension configs are set on the private setting passed to
    `configkey`. The `builtins` keyword accepts a list of extensions which cannot be overridden by
    the user. However, builtins can be duplicated to define config options for them if desired."""

    def __init__(
        self,
        builtins: t.Optional[List[str]] = None,
        configkey: str = 'mdx_configs',
        default: List[str] = [],
        **kwargs,
    ):
        super().__init__(default=default, **kwargs)
        self.builtins = builtins or []
        self.configkey = configkey

    def validate_ext_cfg(self, ext, cfg):
        if not isinstance(ext, str):
            raise ValidationError(f"'{ext}' is not a valid Markdown Extension name.")
        if not cfg:
            return
        if not isinstance(cfg, dict):
            raise ValidationError(f"Invalid config options for Markdown Extension '{ext}'.")
        self.configdata[ext] = cfg

    def run_validation(self, value):
        self.configdata = {}
        if not isinstance(value, (list, tuple, dict)):
            raise ValidationError('Invalid Markdown Extensions configuration')
        extensions = []
        if isinstance(value, dict):
            for ext, cfg in value.items():
                self.validate_ext_cfg(ext, cfg)
                extensions.append(ext)
        else:
            for item in value:
                if isinstance(item, dict):
                    if len(item) > 1:
                        raise ValidationError('Invalid Markdown Extensions configuration')
                    ext, cfg = item.popitem()
                    self.validate_ext_cfg(ext, cfg)
                    extensions.append(ext)
                elif isinstance(item, str):
                    extensions.append(item)
                else:
                    raise ValidationError('Invalid Markdown Extensions configuration')

        extensions = utils.reduce_list(self.builtins + extensions)

        # Confirm that Markdown considers extensions to be valid
        md = markdown.Markdown()
        for ext in extensions:
            try:
                md.registerExtensions((ext,), self.configdata)
            except Exception as e:
                stack = []
                for frame in reversed(traceback.extract_tb(sys.exc_info()[2])):
                    if not frame.line:  # Ignore frames before <frozen importlib._bootstrap>
                        break
                    stack.insert(0, frame)
                tb = ''.join(traceback.format_list(stack))

                raise ValidationError(
                    f"Failed to load extension '{ext}'.\n{tb}{type(e).__name__}: {e}"
                )

        return extensions

    def post_validation(self, config, key_name):
        config[self.configkey] = self.configdata


class Plugins(OptionallyRequired[plugins.PluginCollection]):
    """
    Plugins config option.

    A list or dict of plugins. If a plugin defines config options those are used when
    initializing the plugin class.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.installed_plugins = plugins.get_plugins()
        self.config_file_path = None
        self.plugin_cache: Dict[str, plugins.BasePlugin] = {}

    def pre_validation(self, config, key_name):
        self.config_file_path = config.config_file_path

    def run_validation(self, value):
        if not isinstance(value, (list, tuple, dict)):
            raise ValidationError('Invalid Plugins configuration. Expected a list or dict.')
        self.plugins = plugins.PluginCollection()
        if isinstance(value, dict):
            for name, cfg in value.items():
                self.plugins[name] = self.load_plugin(name, cfg)
        else:
            for item in value:
                if isinstance(item, dict):
                    if len(item) > 1:
                        raise ValidationError('Invalid Plugins configuration')
                    name, cfg = item.popitem()
                    item = name
                else:
                    cfg = {}
                self.plugins[item] = self.load_plugin(item, cfg)
        return self.plugins

    def load_plugin(self, name, config):
        if not isinstance(name, str):
            raise ValidationError(f"'{name}' is not a valid plugin name.")
        if name not in self.installed_plugins:
            raise ValidationError(f'The "{name}" plugin is not installed')

        config = config or {}  # Users may define a null (None) config
        if not isinstance(config, dict):
            raise ValidationError(f"Invalid config options for the '{name}' plugin.")

        try:
            plugin = self.plugin_cache[name]
        except KeyError:
            Plugin = self.installed_plugins[name].load()

            if not issubclass(Plugin, plugins.BasePlugin):
                raise ValidationError(
                    f'{Plugin.__module__}.{Plugin.__name__} must be a subclass of'
                    f' {plugins.BasePlugin.__module__}.{plugins.BasePlugin.__name__}'
                )

            plugin = Plugin()

            if hasattr(plugin, 'on_startup') or hasattr(plugin, 'on_shutdown'):
                self.plugin_cache[name] = plugin

        errors, warnings = plugin.load_config(config, self.config_file_path)
        self.warnings.extend(warnings)
        errors_message = '\n'.join(f"Plugin '{name}' value: '{x}'. Error: {y}" for x, y in errors)
        if errors_message:
            raise ValidationError(errors_message)
        return plugin


class Hooks(ListOfItems):
    """A list of Python scripts to be treated as instances of plugins."""

    def __init__(self, plugins_key: str):
        super().__init__(File(exists=True), default=[])
        self.plugins_key = plugins_key

    def run_validation(self, value):
        paths = super().run_validation(value)
        hooks = {}
        for name, path in zip(value, paths):
            hooks[name] = self._load_hook(name, path)
        return hooks

    @functools.lru_cache(maxsize=None)
    def _load_hook(self, name, path):
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None:
            raise ValidationError(f"Cannot import path '{path}' as a Python module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def post_validation(self, config, key_name):
        plugins = config[self.plugins_key]
        for name, hook in config[key_name].items():
            plugins[name] = hook
