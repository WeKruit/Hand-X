"""AES-256-GCM credential decryption — supports both GH envelope and VALET formats.

GH envelope format (encryption.ts):
	base64( version:1 + keyId:2 + iv:12 + authTag:16 + ciphertext:* )

VALET format (valetCredentialEncryption.ts):
	base64( iv:16 + authTag:16 + ciphertext:* )
	Key derived via scrypt(password, 'valet-cred-salt', 32)

Both use AES-256-GCM. The GH format includes a version byte and key ID for
rotation support; the VALET format is simpler (single key, scrypt-derived).
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt as _CryptoScrypt

# ── Constants ────────────────────────────────────────────────────────────

# GH envelope constants (from encryption.ts)
GH_IV_BYTES = 12
GH_AUTH_TAG_BYTES = 16
GH_KEY_BYTES = 32
GH_ENVELOPE_VERSION = 1
GH_ENVELOPE_HEADER_BYTES = 1 + 2 + GH_IV_BYTES + GH_AUTH_TAG_BYTES  # 31

# VALET encryption constants (from valetCredentialEncryption.ts)
VALET_IV_BYTES = 16
VALET_AUTH_TAG_BYTES = 16
VALET_CREDENTIAL_SALT = b"valet-cred-salt"


# ── GH envelope format ──────────────────────────────────────────────────


def decrypt_gh_envelope(
	ciphertext_b64: str,
	key_hex: str,
	previous_keys: dict[int, str] | None = None,
) -> str:
	"""Decrypt a GH-format encrypted credential envelope.

	Envelope layout:
		[version:1 byte][keyId:2 bytes BE][iv:12 bytes][authTag:16 bytes][ciphertext:*]

	Args:
		ciphertext_b64: Base64-encoded envelope.
		key_hex: Primary key as 64 hex characters (32 bytes).
		previous_keys: Optional mapping of key_id -> hex_key for rotation.

	Returns:
		Decrypted plaintext string.

	Raises:
		ValueError: If the envelope is malformed or the key is not found.
	"""
	envelope = base64.b64decode(ciphertext_b64)

	if len(envelope) < GH_ENVELOPE_HEADER_BYTES:
		raise ValueError(
			f"GH envelope too short: {len(envelope)} bytes "
			f"(minimum {GH_ENVELOPE_HEADER_BYTES})"
		)

	# Parse header
	version = envelope[0]
	if version != GH_ENVELOPE_VERSION:
		raise ValueError(f"Unsupported GH envelope version: {version}")

	key_id = struct.unpack(">H", envelope[1:3])[0]
	iv = envelope[3:3 + GH_IV_BYTES]
	auth_tag = envelope[3 + GH_IV_BYTES:3 + GH_IV_BYTES + GH_AUTH_TAG_BYTES]
	encrypted = envelope[GH_ENVELOPE_HEADER_BYTES:]

	# Resolve key
	all_keys: dict[int, str] = {1: key_hex}
	if previous_keys:
		all_keys.update(previous_keys)

	hex_key = all_keys.get(key_id)
	if hex_key is None:
		raise ValueError(
			f"GH encryption key {key_id} not found "
			f"(available: {list(all_keys.keys())})"
		)

	if len(hex_key) != GH_KEY_BYTES * 2:
		raise ValueError(
			f"GH key must be {GH_KEY_BYTES * 2} hex chars, got {len(hex_key)}"
		)

	key = bytes.fromhex(hex_key)

	# Decrypt — AESGCM expects ciphertext + tag concatenated
	aesgcm = AESGCM(key)
	ct_with_tag = encrypted + auth_tag
	plaintext = aesgcm.decrypt(iv, ct_with_tag, None)

	return plaintext.decode("utf-8")


# ── VALET format ─────────────────────────────────────────────────────────


def _derive_valet_key(password: str) -> bytes:
	"""Derive a 32-byte AES key from a password using scrypt.

	Matches Node.js ``crypto.scryptSync(key, 'valet-cred-salt', 32)`` with
	the default Node.js scrypt parameters: N=16384, r=8, p=1.
	"""
	kdf = _CryptoScrypt(
		salt=VALET_CREDENTIAL_SALT,
		length=32,
		n=16384,
		r=8,
		p=1,
	)
	return kdf.derive(password.encode("utf-8"))


def decrypt_valet_credential(encoded: str, encryption_key: str) -> str:
	"""Decrypt a VALET-format encrypted credential.

	Format: base64( iv:16 + authTag:16 + ciphertext:* )
	Key: scrypt(encryption_key, 'valet-cred-salt', 32)

	This matches the ``decryptValetPlatformCredentialSecret()`` function
	from GH's ``valetCredentialEncryption.ts``.

	Args:
		encoded: Base64-encoded payload.
		encryption_key: Raw password string (CREDENTIAL_ENCRYPTION_KEY env var).

	Returns:
		Decrypted plaintext string.
	"""
	payload = base64.b64decode(encoded)

	if len(payload) < VALET_IV_BYTES + VALET_AUTH_TAG_BYTES + 1:
		raise ValueError(
			f"VALET credential payload too short: {len(payload)} bytes"
		)

	iv = payload[:VALET_IV_BYTES]
	auth_tag = payload[VALET_IV_BYTES:VALET_IV_BYTES + VALET_AUTH_TAG_BYTES]
	ciphertext = payload[VALET_IV_BYTES + VALET_AUTH_TAG_BYTES:]

	key = _derive_valet_key(encryption_key)

	# AESGCM expects ciphertext + tag concatenated
	aesgcm = AESGCM(key)
	ct_with_tag = ciphertext + auth_tag
	plaintext = aesgcm.decrypt(iv, ct_with_tag, None)

	return plaintext.decode("utf-8")


# ── Convenience ──────────────────────────────────────────────────────────


def decrypt_credentials(
	encrypted_data: str,
	key_hex: str,
) -> dict[str, str]:
	"""Decrypt AES-256-GCM encrypted credentials (GH envelope format).

	This is the primary decryption entry point. Expects the GH envelope
	format where the plaintext is a JSON object mapping field names to values
	(e.g. ``{"email": "...", "password": "..."}``).

	If the plaintext is not valid JSON, wraps it as ``{"password": plaintext}``.

	Args:
		encrypted_data: Base64-encoded GH envelope.
		key_hex: 64 hex characters (32 bytes).

	Returns:
		Dictionary of credential field name -> plaintext value.
	"""
	plaintext = decrypt_gh_envelope(encrypted_data, key_hex)
	try:
		parsed = json.loads(plaintext)
		if isinstance(parsed, dict):
			return {str(k): str(v) for k, v in parsed.items()}
		return {"password": plaintext}
	except json.JSONDecodeError:
		return {"password": plaintext}


def decrypt_valet_credentials(
	encrypted_data: str,
	encryption_key: str,
) -> dict[str, str]:
	"""Decrypt VALET-format encrypted credentials.

	Expects the plaintext to be a JSON object or a raw password string.

	Args:
		encrypted_data: Base64-encoded VALET payload.
		encryption_key: CREDENTIAL_ENCRYPTION_KEY env var value.

	Returns:
		Dictionary of credential field name -> plaintext value.
	"""
	plaintext = decrypt_valet_credential(encrypted_data, encryption_key)
	try:
		parsed = json.loads(plaintext)
		if isinstance(parsed, dict):
			return {str(k): str(v) for k, v in parsed.items()}
		return {"password": plaintext}
	except json.JSONDecodeError:
		return {"password": plaintext}
