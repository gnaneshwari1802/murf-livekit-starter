import pytest

from agent import Assistant
from caller_memory import CallerMemoryStore


def test_memory_persists_and_updates_a_caller(tmp_path) -> None:
    database = tmp_path / "caller_memory.db"
    store = CallerMemoryStore(database)
    store.save(
        user_id="caller-42",
        name="Ramesh",
        language_preference="Hindi",
        facts={"age_band": "35-44", "ongoing_conditions": "asthma"},
    )

    # A fresh store represents a fully restarted agent process.
    restarted_store = CallerMemoryStore(database)
    record = restarted_store.lookup("caller-42")

    assert record is not None
    assert record["user_id"] == "caller-42"
    assert record["name"] == "Ramesh"
    assert record["language_preference"] == "Hindi"
    assert record["facts"] == {"age_band": "35-44", "ongoing_conditions": "asthma"}
    assert record["last_interaction"]


def test_no_record_exists_until_consent_is_saved(tmp_path) -> None:
    store = CallerMemoryStore(tmp_path / "caller_memory.db")

    assert store.lookup("caller-43") is None


@pytest.mark.asyncio
async def test_save_tool_refuses_to_store_data_without_explicit_consent(
    tmp_path,
) -> None:
    store = CallerMemoryStore(tmp_path / "caller_memory.db")
    assistant = Assistant(caller_id="caller-44", memory_store=store)

    response = await assistant.save_caller_memory(
        None,
        name="Meera",
        consent_confirmed=False,
        age_band="25-34",
    )

    assert "not saved" in response
    assert store.lookup("caller-44") is None
