from app.repositories.record_repository import check_database_connection, list_records


async def get_database_status() -> dict[str, str]:
    return await check_database_connection()


async def get_commitments() -> list[dict]:
    return await list_records("commitments")


async def get_decisions() -> list[dict]:
    return await list_records("decisions")


async def get_memories() -> list[dict]:
    return await list_records("memories")


async def get_stored_messages() -> list[dict]:
    return await list_records("messages")
