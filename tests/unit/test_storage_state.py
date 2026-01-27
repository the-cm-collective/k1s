import base64

from ae.apishim.store import ObjectStore
from ae.storage.state import ApishimStorageState


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def test_apishim_storage_state_decodes_secret_data(tmp_path) -> None:
    store = ObjectStore(db_path=tmp_path / "apishim.db")
    store.upsert(
        "",
        "v1",
        "secrets",
        "demo",
        "creds",
        {"name": "creds", "namespace": "demo"},
        {"username": _b64("user"), "password": _b64("pass")},
        status={},
    )
    state = ApishimStorageState(store)
    secret = state.get_secret("demo", "creds")
    assert secret == {"username": "user", "password": "pass"}

