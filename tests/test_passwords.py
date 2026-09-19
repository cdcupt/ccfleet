"""Password hashing. Standard library only, so the details matter more than usual."""
import pytest

from ccfleetd.passwords import generate_password, hash_password, verify_password


def test_a_hash_is_self_describing_and_salted():
    a, b = hash_password("same password"), hash_password("same password")
    assert a != b, "two hashes of one password must differ, or the salt is not doing its job"
    algorithm, iterations, salt, digest = a.split("$")
    assert algorithm == "pbkdf2_sha256"
    assert int(iterations) >= 600_000, "OWASP's floor for this algorithm"
    assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32


def test_the_password_itself_never_appears_in_the_hash():
    secret = "hunter2-correct-horse"
    assert secret not in hash_password(secret)


def test_verification_accepts_the_right_password_and_nothing_else():
    stored = hash_password("right")
    assert verify_password("right", stored)
    for wrong in ("wrong", "", "Right", "right "):
        assert not verify_password(wrong, stored)


def test_a_stored_value_that_is_junk_is_false_rather_than_an_exception():
    """These come out of a database; a corrupt row must not take the server down."""
    for junk in ("", "garbage", "pbkdf2_sha256$notanumber$aa$bb", "pbkdf2_sha256$1$zz$zz",
                 "argon2$1$aa$bb", "pbkdf2_sha256$0$aa$bb", None):
        assert verify_password("anything", junk) is False


def test_an_empty_password_is_refused_at_the_door():
    with pytest.raises(ValueError):
        hash_password("")


def test_generated_passwords_are_the_requested_length_and_not_repeated():
    assert len(generate_password()) == 20
    assert len({generate_password() for _ in range(50)}) == 50


def test_a_stored_iteration_count_cannot_be_used_to_burn_cpu_or_crash():
    """The cost is attacker-controlled once a row can be edited or restored."""
    import time

    from ccfleetd.passwords import MAX_ITERATIONS
    salt, digest = "aa" * 16, "bb" * 32
    # Enormous: used to raise OverflowError straight out of pbkdf2_hmac.
    assert verify_password("x", f"pbkdf2_sha256$99999999999999999999${salt}${digest}") is False
    # Merely large: used to cost 1.4s of CPU per request.
    started = time.monotonic()
    assert verify_password("x", f"pbkdf2_sha256$20000000${salt}${digest}") is False
    assert time.monotonic() - started < 0.5, "a rejected hash must not do the work"
    # Zero and negative are refused too.
    for bad in ("0", "-1"):
        assert verify_password("x", f"pbkdf2_sha256${bad}${salt}${digest}") is False
    # And the bound still leaves room to raise the real cost later.
    assert MAX_ITERATIONS > 600_000
