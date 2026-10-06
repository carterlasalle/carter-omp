"""`carter-omp add-org`: onboarding another account/org without re-setup."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from carter_omp import app_auth
from carter_omp.cli import (
    _app_slug,
    _installation_for_owner,
    _merge_csv_env,
    _read_env_file,
    _upsert_env_file,
)


def test_merge_csv_env_is_order_stable_and_idempotent() -> None:
    assert _merge_csv_env("carterlasalle", ["neworg"]) == "carterlasalle,neworg"
    # Re-running onboarding must not grow the list, and case is not a new owner.
    assert _merge_csv_env("carterlasalle,neworg", ["NewOrg"]) == "carterlasalle,neworg"
    assert _merge_csv_env("", ["neworg", " ", "neworg"]) == "neworg"
    assert _merge_csv_env("111,222", ["222", "333"]) == "111,222,333"


def test_upsert_env_file_preserves_comments_and_is_idempotent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# owner scope\nCARTER_OMP_REPO_OWNERS=carterlasalle\n\nCARTER_OMP_MODEL=a/b\n",
        encoding="utf-8",
    )

    changed = _upsert_env_file(env, {"CARTER_OMP_REPO_OWNERS": "carterlasalle,neworg"})

    assert changed == ["CARTER_OMP_REPO_OWNERS"]
    text = env.read_text(encoding="utf-8")
    assert "# owner scope" in text  # comments survive
    assert "CARTER_OMP_MODEL=a/b" in text  # unrelated keys untouched
    assert "CARTER_OMP_REPO_OWNERS=carterlasalle,neworg" in text

    # Second run with the same value changes nothing.
    assert _upsert_env_file(env, {"CARTER_OMP_REPO_OWNERS": "carterlasalle,neworg"}) == []
    # A key that does not exist yet is appended.
    assert _upsert_env_file(env, {"CARTER_OMP_GITHUB_INSTALLATION_ID": "9"}) == ["CARTER_OMP_GITHUB_INSTALLATION_ID"]
    assert env.read_text(encoding="utf-8").rstrip().endswith("CARTER_OMP_GITHUB_INSTALLATION_ID=9")


def _install_transport(routes: dict[str, int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        for path, installation_id in routes.items():
            if request.url.path == path:
                return httpx.Response(200, json={"id": installation_id, "slug": "carter-omp"})
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.MockTransport(handler)


def test_installation_for_owner_prefers_org_then_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_auth, "mint_app_jwt", lambda **_k: "jwt")

    org = _installation_for_owner(
        "neworg", app_id="1", private_key_pem="pem", transport=_install_transport({"/orgs/neworg/installation": 77})
    )
    assert org == 77

    # User accounts answer on /users/<login>/installation.
    user = _installation_for_owner(
        "someone", app_id="1", private_key_pem="pem", transport=_install_transport({"/users/someone/installation": 88})
    )
    assert user == 88

    # Not installed anywhere -> None (the command then prints the install link).
    assert _installation_for_owner("ghost", app_id="1", private_key_pem="pem", transport=_install_transport({})) is None


def test_app_slug_reads_the_app_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_auth, "mint_app_jwt", lambda **_k: "jwt")
    assert _app_slug(app_id="1", private_key_pem="pem", transport=_install_transport({"/app": 0})) == "carter-omp"


def test_read_env_file_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# c\n\nA=1\nB = 2 \n", encoding="utf-8")
    assert _read_env_file(env) == {"A": "1", "B": "2"}
    assert _read_env_file(tmp_path / "missing.env") == {}
