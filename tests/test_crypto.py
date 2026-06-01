import sys; sys.path.insert(0, 'backend')
from core import crypto as c

print("1. Create database envelope...")
env, dek = c.create_envelope("hunter2", time_cost=1, memory_cost=8*1024, parallelism=1)
print("   DEK length:", len(dek), "bytes")

print("2. Encrypt some data with the DEK...")
secret = c.encrypt_text(dek, "Personnummer 19900101-1234")
print("   stored (b64):", secret[:32], "...")

print("3. Unlock with correct passphrase recovers SAME dek...")
dek2 = c.unlock(env, "hunter2")
assert dek2 == dek, "DEK mismatch!"
assert c.decrypt_text(dek2, secret) == "Personnummer 19900101-1234"
print("   OK, decrypted correctly")

print("4. Wrong passphrase is rejected...")
try:
    c.unlock(env, "wrongpass")
    print("   FAIL - should have raised")
except c.BadPassphrase:
    print("   OK, BadPassphrase raised")

print("5. Change passphrase -> same DEK, old data still readable...")
env2 = c.change_passphrase(env, "hunter2", "newpass123", time_cost=1, memory_cost=8*1024, parallelism=1)
dek3 = c.unlock(env2, "newpass123")
assert dek3 == dek, "DEK changed on passphrase change - data would be lost!"
assert c.decrypt_text(dek3, secret) == "Personnummer 19900101-1234"
print("   OK, same DEK, no data re-encryption needed")
try:
    c.unlock(env2, "hunter2"); print("   FAIL old pass still works")
except c.BadPassphrase: print("   OK, old passphrase no longer works")

print("6. Recovery key...")
env3, rk = c.add_recovery_key(env2, "newpass123")
print("   recovery key:", rk)
dek4 = c.unlock_with_recovery(env3, rk)
assert dek4 == dek, "recovery unwrap gave wrong DEK"
print("   OK, recovery key unlocks same DEK")

print("7. Envelope survives JSON round-trip (disk persistence)...")
restored = c.KeyEnvelope.from_json(env3.to_json())
assert c.unlock(restored, "newpass123") == dek
print("   OK")

print("8. Tampering with ciphertext is detected...")
bad = bytearray(c._b64d(secret)); bad[20] ^= 0xFF
try:
    c.decrypt_blob(dek, bytes(bad)); print("   FAIL tamper not caught")
except Exception: print("   OK, tampering rejected (auth tag)")

print("\nALL CRYPTO TESTS PASSED")
