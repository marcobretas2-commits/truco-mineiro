import sqlite3
import secrets
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "usuarios.db"


def conectar():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def inicializar_banco():
    conn = conectar()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            codigo_recuperacao TEXT,
            codigo_expira TEXT,
            criado_em TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def gerar_hash_senha(senha):
    return hashlib.sha256(senha.encode("utf-8")).hexdigest()


def verificar_senha(senha, senha_hash):
    return gerar_hash_senha(senha) == senha_hash


def criar_usuario(nome, email, senha):
    email = email.strip().lower()

    conn = conectar()

    try:
        conn.execute(
            """
            INSERT INTO usuarios
            (nome, email, senha_hash, criado_em)
            VALUES (?, ?, ?, ?)
            """,
            (
                nome.strip(),
                email,
                gerar_hash_senha(senha),
                datetime.now().isoformat()
            )
        )

        conn.commit()

        usuario_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        return {
            "ok": True,
            "id": usuario_id
        }

    except sqlite3.IntegrityError:
        return {
            "ok": False,
            "erro": "Este e-mail já está cadastrado."
        }

    finally:
        conn.close()


def autenticar_usuario(email, senha):
    email = email.strip().lower()

    conn = conectar()

    usuario = conn.execute(
        """
        SELECT id, nome, email, senha_hash
        FROM usuarios
        WHERE email = ?
        """,
        (email,)
    ).fetchone()

    conn.close()

    if not usuario:
        return None

    if not verificar_senha(senha, usuario["senha_hash"]):
        return None

    return {
        "id": usuario["id"],
        "nome": usuario["nome"],
        "email": usuario["email"]
    }


def gerar_codigo_recuperacao(email):
    email = email.strip().lower()
    codigo = str(secrets.randbelow(1000000)).zfill(6)

    conn = conectar()

    usuario = conn.execute(
        "SELECT id FROM usuarios WHERE email = ?",
        (email,)
    ).fetchone()

    if not usuario:
        conn.close()
        return None

    expiracao = datetime.now() + timedelta(minutes=15)

    conn.execute(
        """
        UPDATE usuarios
        SET codigo_recuperacao = ?,
            codigo_expira = ?
        WHERE email = ?
        """,
        (
            codigo,
            expiracao.isoformat(),
            email
        )
    )

    conn.commit()
    conn.close()

    return codigo


def redefinir_senha(email, codigo, nova_senha):
    email = email.strip().lower()

    conn = conectar()

    usuario = conn.execute(
        """
        SELECT id, codigo_expira
        FROM usuarios
        WHERE email = ?
          AND codigo_recuperacao = ?
        """,
        (email, codigo)
    ).fetchone()

    if not usuario:
        conn.close()
        return {
            "ok": False,
            "erro": "Código de recuperação inválido."
        }

    try:
        expiracao = datetime.fromisoformat(usuario["codigo_expira"])
    except Exception:
        conn.close()
        return {
            "ok": False,
            "erro": "Código de recuperação inválido."
        }

    if datetime.now() > expiracao:
        conn.close()
        return {
            "ok": False,
            "erro": "O código de recuperação expirou."
        }

    conn.execute(
        """
        UPDATE usuarios
        SET senha_hash = ?,
            codigo_recuperacao = NULL,
            codigo_expira = NULL
        WHERE id = ?
        """,
        (
            gerar_hash_senha(nova_senha),
            usuario["id"]
        )
    )

    conn.commit()
    conn.close()

    return {
        "ok": True
    }


inicializar_banco()
