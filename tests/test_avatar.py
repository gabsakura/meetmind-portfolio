from __future__ import annotations

import io
import struct
import zlib

from app.extensions import db
from app.models import User
from tests.conftest import login


def png_bytes(size_bytes: int = 0) -> bytes:
    """PNG 1x1 válido. size_bytes infla o arquivo com um chunk de comentário."""

    def chunk(tipo: bytes, dados: bytes) -> bytes:
        return (
            struct.pack(">I", len(dados))
            + tipo
            + dados
            + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF)
        )

    cabecalho = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\xff\xff\xff")
    saida = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", cabecalho) + chunk(b"IDAT", pixel)
    if size_bytes:
        saida += chunk(b"teXt", b"x" * size_bytes)
    return saida + chunk(b"IEND", b"")


def enviar(client, dados: bytes, nome: str = "foto.png"):
    return client.post(
        "/perfil/foto",
        data={"foto": (io.BytesIO(dados), nome)},
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def test_upload_salva_e_serve_a_foto(client, app):
    login(client)
    resposta = enviar(client, png_bytes())
    assert resposta.status_code == 302

    with app.app_context():
        user = User.query.filter_by(email="admin@example.test").one()
        assert user.avatar_mime == "image/png"
        assert user.has_avatar is True
        user_id = user.id

    imagem = client.get(f"/perfil/foto/{user_id}")
    assert imagem.status_code == 200
    assert imagem.mimetype == "image/png"
    assert imagem.data.startswith(b"\x89PNG")


def test_formato_nao_aceito_e_recusado(client, app):
    login(client)
    enviar(client, b"GIF89a" + b"\x00" * 40, "foto.gif")
    with app.app_context():
        user = User.query.filter_by(email="admin@example.test").one()
        assert user.avatar_mime is None


def test_svg_e_recusado_mesmo_com_nome_de_png(client, app):
    login(client)
    enviar(client, b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>', "foto.png")
    with app.app_context():
        user = User.query.filter_by(email="admin@example.test").one()
        assert user.avatar_mime is None


def test_acima_do_limite_e_recusado(client, app):
    app.config["AVATAR_MAX_BYTES"] = 2048
    login(client)
    enviar(client, png_bytes(size_bytes=4096))
    with app.app_context():
        user = User.query.filter_by(email="admin@example.test").one()
        assert user.avatar_mime is None


def test_remover_volta_para_as_iniciais(client, app):
    login(client)
    enviar(client, png_bytes())
    resposta = client.post("/perfil/foto/remover")
    assert resposta.status_code == 302
    with app.app_context():
        user = User.query.filter_by(email="admin@example.test").one()
        assert user.avatar_mime is None
        assert user.avatar_data is None
        assert user.has_avatar is False
        assert user.initials == "A"


def test_foto_exige_sessao(client, app):
    login(client)
    enviar(client, png_bytes())
    with app.app_context():
        user_id = User.query.filter_by(email="admin@example.test").one().id
    client.post("/auth/logout")
    anonima = client.get(f"/perfil/foto/{user_id}", follow_redirects=False)
    assert anonima.status_code == 302
    assert "/auth/login" in anonima.headers["Location"]


def test_pagina_de_perfil_abre(client):
    login(client)
    pagina = client.get("/perfil")
    assert pagina.status_code == 200
    assert "Foto de perfil" in pagina.get_data(as_text=True)
    assert "Integrar calendário" in pagina.get_data(as_text=True)


def test_menu_do_avatar_abre_integracao_de_calendario(client):
    login(client)
    pagina = client.get("/")
    html = pagina.get_data(as_text=True)

    assert "Abrir menu do perfil" in html
    assert 'href="/perfil#calendario"' in html

    perfil = client.get("/perfil")
    assert "Microsoft 365" in perfil.get_data(as_text=True)
    assert "Google Calendar" in perfil.get_data(as_text=True)


def test_iniciais_com_nome_composto(app):
    with app.app_context():
        user = User(email="x@example.test", name="maria da silva souza")
        db.session.add(user)
        assert user.initials == "MS"
        db.session.rollback()
