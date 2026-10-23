"""Dedicated SSH key for the app.

The app keeps its own ed25519 key inside DATA_DIR (persisted on the TrueNAS dataset)
so it never touches the user's personal ~/.ssh keys. The public half is registered
with the GitHub account that the comma trusts (comma reads github.com/<user>.keys).

Keys are generated and checked in Python (cryptography + paramiko), so no ssh-keygen
binary is needed inside the container.
"""
import base64
import hashlib
import os
import stat
import urllib.error
import urllib.request

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import config

KEY_DIR = os.path.join(config.DATA_DIR, "ssh")
PRIV = os.path.join(KEY_DIR, "id_ed25519")
PUB = PRIV + ".pub"


def ensure_dir():
    os.makedirs(KEY_DIR, mode=0o700, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)


def generate(overwrite: bool = False) -> None:
    """Create a passphrase-less ed25519 key pair. Raises RuntimeError on failure."""
    ensure_dir()
    if os.path.exists(PRIV) and os.path.exists(PUB) and not overwrite:
        return
    try:
        key = Ed25519PrivateKey.generate()
        priv_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        pub_line = key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        ).decode() + " comma-sync"
        # write private key with 600 from the start (no window where it is world-readable)
        fd = os.open(PRIV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(priv_pem)
        with open(PUB, "w") as f:
            f.write(pub_line + "\n")
        os.chmod(PRIV, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        raise RuntimeError(f"Could not write SSH key: {e}") from e


def import_private_key(text: str) -> str:
    """Use an existing ed25519 key pair (for example the one Comma Sync already uses).

    Returns the public key line. Raises RuntimeError if the text isn't a usable key.
    """
    from io import StringIO
    body = (text or "").strip()
    if not body:
        raise RuntimeError("Paste the contents of the private key file.")
    try:
        key = paramiko.Ed25519Key.from_private_key(StringIO(body + "\n"))
    except Exception as e:  # noqa: BLE001 - any parse failure gets the same message
        raise RuntimeError(
            "That isn't an ed25519 private key. Paste the whole file, including the "
            "'-----BEGIN OPENSSH PRIVATE KEY-----' and '-----END OPENSSH PRIVATE KEY-----' lines. "
            f"({e.__class__.__name__})") from e
    pub_line = f"ssh-ed25519 {key.get_base64()} comma-sync-imported"
    ensure_dir()
    fd = os.open(PRIV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body + "\n")
    with open(PUB, "w") as f:
        f.write(pub_line + "\n")
    os.chmod(PRIV, stat.S_IRUSR | stat.S_IWUSR)
    ok, msg = check_local()
    if not ok:
        raise RuntimeError(msg)
    return pub_line


def public_key() -> str | None:
    """The public key line, or None if no key exists yet."""
    if not os.path.exists(PUB):
        return None
    with open(PUB) as f:
        return f.read().strip()


def _blob(pub_line: str) -> bytes | None:
    parts = pub_line.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        return None
    try:
        return base64.b64decode(parts[1])
    except ValueError:
        return None


def check_local() -> tuple[bool, str]:
    """Verify the key pair exists, has safe permissions, and that both halves match."""
    if not os.path.exists(PRIV):
        return False, "No key yet — click Generate key."
    mode = stat.S_IMODE(os.stat(PRIV).st_mode)
    if mode & 0o077:
        try:
            os.chmod(PRIV, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            return False, f"Private key permissions are {oct(mode)}; must be 600."
    try:
        priv = paramiko.Ed25519Key.from_private_key_file(PRIV)
    except (paramiko.SSHException, OSError, ValueError):
        return False, "Private key is unreadable or corrupt — replace the key."
    pub = public_key()
    if pub is None:
        return False, "Public key file missing — replace the key."
    blob = _blob(pub)
    if blob is None:
        return False, "Public key file is not a valid ed25519 key."
    if priv.asbytes() != blob:
        return False, "Public and private key do not match — replace the key."
    return True, "Key pair is valid (ed25519)."


def fingerprint() -> str:
    blob = _blob(public_key() or "")
    if not blob:
        return ""
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def github_registered(github_user: str) -> tuple[bool | None, str]:
    """Is our public key listed at github.com/<user>.keys?

    Returns (True, msg) / (False, msg) / (None, msg) when the check can't run.
    """
    if not github_user:
        return None, "Set your GitHub username in Settings to verify registration."
    pub = public_key()
    if not pub:
        return False, "No key to register yet."
    body_key = pub.split()[1]
    url = f"https://github.com/{github_user}.keys"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            keys = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, f"GitHub user '{github_user}' not found (HTTP {e.code})."
    except (urllib.error.URLError, OSError) as e:
        return None, f"Could not reach GitHub to verify ({e})."
    if body_key in keys:
        return True, f"Key is registered on GitHub account '{github_user}'."
    return False, f"Key is NOT on GitHub account '{github_user}' — add it at github.com/settings/keys."
