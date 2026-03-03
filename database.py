import duckdb

DB_FILE = "bot_data.duckdb"

def get_connection():
    return duckdb.connect(DB_FILE)

def init_db():
    """Initializes the database schema."""
    conn = get_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username VARCHAR,
            eth_address VARCHAR,
            eth_private_key VARCHAR,
            sol_address VARCHAR,
            sol_private_key VARCHAR
        )
    ''')
    conn.close()

def get_user(user_id: int):
    """Fetches a user by their Telegram ID."""
    conn = get_connection()
    result = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if result:
        return {
            "user_id": result[0],
            "username": result[1],
            "eth_address": result[2],
            "eth_private_key": result[3],
            "sol_address": result[4],
            "sol_private_key": result[5]
        }
    return None

def get_user_by_address(eth_address: str):
    """Fetches a user by their EVM wallet address."""
    conn = get_connection()
    result = conn.execute("SELECT * FROM users WHERE eth_address = ?", (eth_address,)).fetchone()
    conn.close()
    if result:
        return {
            "user_id": result[0],
            "username": result[1],
            "eth_address": result[2],
            "eth_private_key": result[3],
            "sol_address": result[4],
            "sol_private_key": result[5]
        }
    return None

def create_user(user_id: int, username: str, eth_data: dict, sol_data):
    """Inserts a new user with their generated wallets."""
    conn = get_connection()
    
    if isinstance(sol_data, dict):
        sol_address = sol_data['address']
        sol_private_key = sol_data['private_key']
    else:
        sol_address = sol_data[0] if sol_data else ""
        sol_private_key = sol_data[1] if len(sol_data) > 1 else ""
    
    conn.execute('''
        INSERT INTO users (user_id, username, eth_address, eth_private_key, sol_address, sol_private_key)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        username,
        eth_data['address'],
        eth_data['private_key'],
        sol_address,
        sol_private_key
    ))
    conn.close()
