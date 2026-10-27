"""冻结的 v2 schema 快照。

该模块只用于 ``legacy_v1_to_v2`` 迁移，避免迁移过程依赖当前运行时代码中的
最新 SQLModel 定义，导致历史迁移随着后续 schema 演进而失真。
"""

from sqlalchemy.engine import Connection

_V2_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS action_records (
        id INTEGER NOT NULL,
        action_id VARCHAR(255) NOT NULL,
        timestamp DATETIME,
        session_id VARCHAR(255) NOT NULL,
        action_name VARCHAR(255) NOT NULL,
        action_reasoning VARCHAR,
        action_data VARCHAR,
        action_builtin_prompt VARCHAR,
        action_display_prompt VARCHAR,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS binary_data (
        id INTEGER NOT NULL,
        data_hash VARCHAR(255) NOT NULL,
        full_path VARCHAR(1024) NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER NOT NULL,
        session_id VARCHAR(255) NOT NULL,
        start_timestamp DATETIME,
        end_timestamp DATETIME,
        query_count INTEGER NOT NULL,
        query_forget_count INTEGER NOT NULL,
        original_messages VARCHAR NOT NULL,
        participants VARCHAR NOT NULL,
        theme VARCHAR NOT NULL,
        keywords VARCHAR NOT NULL,
        summary VARCHAR NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id INTEGER NOT NULL,
        session_id VARCHAR(255) NOT NULL,
        created_timestamp DATETIME,
        last_active_timestamp DATETIME,
        user_id VARCHAR(255),
        group_id VARCHAR(255),
        platform VARCHAR(100) NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS command_records (
        id INTEGER NOT NULL,
        timestamp DATETIME,
        session_id VARCHAR(255) NOT NULL,
        command_name VARCHAR(255) NOT NULL,
        command_data VARCHAR,
        command_result VARCHAR,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS expressions (
        id INTEGER NOT NULL,
        situation VARCHAR(255) NOT NULL,
        style VARCHAR(255) NOT NULL,
        content_list VARCHAR NOT NULL,
        count INTEGER NOT NULL,
        last_active_time DATETIME,
        create_time DATETIME,
        session_id VARCHAR(255),
        checked BOOLEAN NOT NULL,
        rejected BOOLEAN NOT NULL,
        modified_by VARCHAR(4),
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS images (
        id INTEGER NOT NULL,
        image_hash VARCHAR(255) NOT NULL,
        description VARCHAR NOT NULL,
        full_path VARCHAR(1024) NOT NULL,
        image_type VARCHAR(5),
        emotion VARCHAR,
        query_count INTEGER NOT NULL,
        is_registered BOOLEAN NOT NULL,
        is_banned BOOLEAN NOT NULL,
        no_file_flag BOOLEAN NOT NULL,
        record_time DATETIME,
        register_time DATETIME,
        last_used_time DATETIME,
        vlm_processed BOOLEAN NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jargons (
        id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
        content VARCHAR(255) NOT NULL,
        raw_content TEXT,
        meaning TEXT NOT NULL,
        session_id_dict TEXT NOT NULL,
        count INTEGER NOT NULL,
        is_jargon BOOLEAN,
        is_complete BOOLEAN NOT NULL,
        is_global BOOLEAN NOT NULL,
        last_inference_count INTEGER NOT NULL,
        inference_with_context TEXT,
        inference_with_content_only TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_usage (
        id INTEGER NOT NULL,
        model_name VARCHAR(255) NOT NULL,
        model_assign_name VARCHAR(255),
        model_api_provider_name VARCHAR(255) NOT NULL,
        endpoint VARCHAR(255),
        user_type VARCHAR(6),
        request_type VARCHAR(50) NOT NULL,
        time_cost FLOAT,
        timestamp DATETIME,
        prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER NOT NULL,
        total_tokens INTEGER NOT NULL,
        cost FLOAT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mai_messages (
        id INTEGER NOT NULL,
        message_id VARCHAR(255) NOT NULL,
        timestamp DATETIME,
        platform VARCHAR(100) NOT NULL,
        user_id VARCHAR(255) NOT NULL,
        user_nickname VARCHAR(255) NOT NULL,
        user_cardname VARCHAR(255),
        group_id VARCHAR(255),
        group_name VARCHAR(255),
        is_mentioned BOOLEAN NOT NULL,
        is_at BOOLEAN NOT NULL,
        session_id VARCHAR(255) NOT NULL,
        reply_to VARCHAR(255),
        is_emoji BOOLEAN NOT NULL,
        is_picture BOOLEAN NOT NULL,
        is_command BOOLEAN NOT NULL,
        is_notify BOOLEAN NOT NULL,
        raw_content BLOB,
        processed_plain_text VARCHAR,
        display_message VARCHAR,
        additional_config VARCHAR,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS online_time (
        id INTEGER NOT NULL,
        timestamp DATETIME,
        duration_minutes INTEGER NOT NULL,
        start_timestamp DATETIME,
        end_timestamp DATETIME,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS person_info (
        id INTEGER NOT NULL,
        is_known BOOLEAN NOT NULL,
        person_id VARCHAR(255) NOT NULL,
        person_name VARCHAR(255),
        name_reason VARCHAR,
        platform VARCHAR(100) NOT NULL,
        user_id VARCHAR(255) NOT NULL,
        user_nickname VARCHAR(255) NOT NULL,
        group_cardname VARCHAR,
        memory_points VARCHAR,
        know_counts INTEGER NOT NULL,
        first_known_time DATETIME,
        last_known_time DATETIME,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS thinking_questions (
        id INTEGER NOT NULL,
        question VARCHAR NOT NULL,
        context VARCHAR,
        found_answer BOOLEAN NOT NULL,
        answer VARCHAR,
        thinking_steps VARCHAR,
        created_timestamp DATETIME,
        updated_timestamp DATETIME,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_records (
        id INTEGER NOT NULL,
        tool_id VARCHAR(255) NOT NULL,
        timestamp DATETIME,
        session_id VARCHAR(255) NOT NULL,
        tool_name VARCHAR(255) NOT NULL,
        tool_reasoning VARCHAR,
        tool_data VARCHAR,
        tool_builtin_prompt VARCHAR,
        tool_display_prompt VARCHAR,
        PRIMARY KEY (id)
    )
    """,
)

_V2_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS ix_action_records_action_id ON action_records (action_id)",
    "CREATE INDEX IF NOT EXISTS ix_action_records_action_name ON action_records (action_name)",
    "CREATE INDEX IF NOT EXISTS ix_action_records_session_id ON action_records (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_action_records_timestamp ON action_records (timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_binary_data_data_hash ON binary_data (data_hash)",
    "CREATE INDEX IF NOT EXISTS ix_chat_history_end_timestamp ON chat_history (end_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_chat_history_session_id ON chat_history (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_chat_history_start_timestamp ON chat_history (start_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_created_timestamp ON chat_sessions (created_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_group_id ON chat_sessions (group_id)",
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_last_active_timestamp ON chat_sessions (last_active_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_platform ON chat_sessions (platform)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_sessions_session_id ON chat_sessions (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_id ON chat_sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_command_records_command_name ON command_records (command_name)",
    "CREATE INDEX IF NOT EXISTS ix_command_records_session_id ON command_records (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_command_records_timestamp ON command_records (timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_expressions_last_active_time ON expressions (last_active_time)",
    "CREATE INDEX IF NOT EXISTS ix_expressions_situation ON expressions (situation)",
    "CREATE INDEX IF NOT EXISTS ix_expressions_style ON expressions (style)",
    "CREATE INDEX IF NOT EXISTS ix_images_image_hash ON images (image_hash)",
    "CREATE INDEX IF NOT EXISTS ix_images_record_time ON images (record_time)",
    "CREATE INDEX IF NOT EXISTS ix_jargons_content ON jargons (content)",
    "CREATE INDEX IF NOT EXISTS ix_llm_usage_model_api_provider_name ON llm_usage (model_api_provider_name)",
    "CREATE INDEX IF NOT EXISTS ix_llm_usage_model_assign_name ON llm_usage (model_assign_name)",
    "CREATE INDEX IF NOT EXISTS ix_llm_usage_model_name ON llm_usage (model_name)",
    "CREATE INDEX IF NOT EXISTS ix_llm_usage_timestamp ON llm_usage (timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_mai_messages_group_id ON mai_messages (group_id)",
    "CREATE INDEX IF NOT EXISTS ix_mai_messages_message_id ON mai_messages (message_id)",
    "CREATE INDEX IF NOT EXISTS ix_mai_messages_platform ON mai_messages (platform)",
    "CREATE INDEX IF NOT EXISTS ix_mai_messages_session_id ON mai_messages (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_mai_messages_user_id ON mai_messages (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_mai_messages_user_nickname ON mai_messages (user_nickname)",
    "CREATE INDEX IF NOT EXISTS ix_online_time_timestamp ON online_time (timestamp)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_person_info_person_id ON person_info (person_id)",
    "CREATE INDEX IF NOT EXISTS ix_person_info_platform ON person_info (platform)",
    "CREATE INDEX IF NOT EXISTS ix_person_info_user_id ON person_info (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_person_info_user_nickname ON person_info (user_nickname)",
    "CREATE INDEX IF NOT EXISTS ix_thinking_questions_created_timestamp ON thinking_questions (created_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_thinking_questions_updated_timestamp ON thinking_questions (updated_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_tool_records_session_id ON tool_records (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_tool_records_timestamp ON tool_records (timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_tool_records_tool_id ON tool_records (tool_id)",
    "CREATE INDEX IF NOT EXISTS ix_tool_records_tool_name ON tool_records (tool_name)",
)


def create_frozen_v2_schema(connection: Connection) -> None:
    """创建冻结的 v2 schema。

    Args:
        connection: 当前数据库连接。
    """

    for statement in _V2_TABLE_STATEMENTS:
        connection.exec_driver_sql(statement)

    for statement in _V2_INDEX_STATEMENTS:
        connection.exec_driver_sql(statement)
