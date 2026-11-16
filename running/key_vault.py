"""Fernet envelope encryption. OS keyring holds master; fallback file is 0600.
Fallback protects against accidental DB disclosure, NOT full host compromise.
GARMIN_KEY_SECRET accepts a Fernet key and is preferred for portable deployment.
"""
import os
import stat
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken


class KeyVault:
    def __init__(self, directory, use_keyring=True):
        self.directory = Path(directory)
        self.use_keyring = use_keyring
        self.backend = 'uninitialized'

    def _master(self):
        configured = os.environ.get('GARMIN_KEY_SECRET')
        if configured:
            self.backend = 'environment'
            return configured.encode('ascii')
        if self.use_keyring:
            try:
                import keyring
                backend = keyring.get_keyring()
                # Reject plaintext/third-party backends; do not silently trust priority.
                if type(backend).__module__ in ('keyring.backends.macOS','keyring.backends.SecretService','keyring.backends.Windows'):
                    service = 'poserun.garmin.' + str(self.directory.resolve())
                    key = backend.get_password(service,'master')
                    if key is None:
                        key = Fernet.generate_key().decode('ascii')
                        backend.set_password(service,'master',key)
                    self.backend = 'os-keyring'
                    return key.encode('ascii')
            except Exception:
                # No raw keyring exceptions: backend may include credential context.
                self.backend = 'file-fallback'
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        path = self.directory/'garmin-vault.key'
        try:
            fd = os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            with os.fdopen(fd,'wb') as stream:
                stream.write(Fernet.generate_key())
        # O_NOFOLLOW prevents reading a symlink supplied in place of the master.
        fd = os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError('钥匙库主密钥文件必须为普通文件且权限为 600')
            key = stream.read(256)
        self.backend = 'file-fallback'
        return key

    def encrypt(self, secret):
        return Fernet(self._master()).encrypt(secret.encode('ascii')).decode('ascii')

    def decrypt(self, encrypted):
        try:
            return Fernet(self._master()).decrypt(encrypted.encode('ascii')).decode('ascii')
        except (InvalidToken, ValueError):
            raise ValueError('钥匙库不可解密；请恢复主密钥或断开后重新连接') from None
