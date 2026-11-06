#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy_github.py — despliegue automático de GoldPilot FN a GitHub (OPCIONAL).

Ejecútalo EN TU ORDENADOR (no en el sandbox), cuando quieras actualizar el repo
sin usar la interfaz web. Para el primer despliegue la vía web (Upload files) es más simple.

Requisitos en tu máquina:
    pip install pynacl requests

Necesita:
  - Un PAT de GitHub fine-grained con permisos sobre el repo:
      Contents: Read/Write · Workflows: Read/Write · (Administration: Read/Write solo si creas el repo)
  - Tu usuario de GitHub

Uso:
    export GITHUB_TOKEN="github_pat_..."
    export GITHUB_USER="tu_usuario"
    export TELEGRAM_BOT_TOKEN="12345:ABC..."
    export TELEGRAM_CHAT_ID="123456"
    export FN_INITIAL_EQUITY="10000"
    export FN_START_DATE="2026-11-03"
    python deploy_github.py --create          # crea el repo (si no existe) + push + secrets + arranque
    python deploy_github.py                   # push + secrets + arranque (repo ya creado)

Crea el repo PÚBLICO a propósito: GitHub Actions es GRATIS e ilimitado solo en repos
públicos (en privados el cupo gratuito de minutos se agota en ~3 días con este ritmo).
El código no contiene secretos: las claves viven cifradas en los Actions Secrets.
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

import requests

API = "https://api.github.com"
REPO_NAME = "goldpilot-fn"
HERE = Path(__file__).resolve().parent.parent  # raíz del repo

REQUIRED_SECRETS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                    "FN_INITIAL_EQUITY", "FN_START_DATE"]


def api(method: str, path: str, token: str, **kwargs) -> requests.Response:
    r = requests.request(
        method, f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30, **kwargs,
    )
    return r


def create_repo(token: str, user: str) -> None:
    r = api("GET", f"/repos/{user}/{REPO_NAME}", token)
    if r.status_code == 200:
        print(f"✓ El repo ya existe: {r.json()['html_url']}")
        return
    r = api("POST", "/user/repos", token, json={
        "name": REPO_NAME,
        "description": "GoldPilot FN — copiloto IA de señales para FundedNext Competition",
        "private": False,
        "has_issues": False, "has_wiki": False, "has_projects": False,
    })
    if r.status_code in (201, 202):
        print(f"✓ Repo creado: {r.json()['html_url']}")
    else:
        sys.exit(f"✗ No pude crear el repo ({r.status_code}): {r.text[:300]}\n"
                 f"  → ¿Tu PAT tiene permiso 'Administration: Read/Write'?")


def git_push(token: str, user: str) -> None:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    remote = f"https://x-access-token:{token}@github.com/{user}/{REPO_NAME}.git"

    def run(*args):
        subprocess.run(args, cwd=HERE, env=env, check=True,
                       capture_output=True, text=True)

    run("git", "init", "-b", "main")
    run("git", "config", "user.name", "GoldPilot Deployer")
    run("git", "config", "user.email", "deploy@users.noreply.github.com")
    run("git", "add", "-A")
    out = subprocess.run(["git", "commit", "-m", "GoldPilot FN deploy"],
                         cwd=HERE, env=env, capture_output=True, text=True)
    if out.returncode not in (0, 1):  # 1 = nada nuevo que commitear
        sys.exit(f"✗ git commit: {out.stderr[:300]}")
    subprocess.run(["git", "remote", "remove", "origin"], cwd=HERE, env=env,
                   capture_output=True)  # ok si no existe
    subprocess.run(["git", "remote", "add", "origin", remote],
                   cwd=HERE, env=env, check=True, capture_output=True)
    run("git", "push", "-u", "origin", "main", "--force")
    # limpiar el token del remote guardado
    subprocess.run(["git", "remote", "set-url", "origin",
                    f"https://github.com/{user}/{REPO_NAME}.git"],
                   cwd=HERE, env=env, check=True, capture_output=True)
    print("✓ Código subido (el token no queda guardado en .git/config)")


def set_secret(token: str, user: str, name: str, value: str) -> None:
    from nacl import encoding, public as nacl_public

    r = api("GET", f"/repos/{user}/{REPO_NAME}/actions/secrets/public-key", token)
    r.raise_for_status()
    data = r.json()
    pk = nacl_public.PublicKey(data["key"].encode(), encoding.Base64Encoder())
    sealed = nacl_public.SealedBox(pk).encrypt(value.encode())
    r = api("PUT", f"/repos/{user}/{REPO_NAME}/actions/secrets/{name}", token, json={
        "encrypted_value": base64.b64encode(sealed).decode(),
        "key_id": data["key_id"],
    })
    if r.status_code in (201, 204):
        print(f"✓ Secret {name} configurado")
    else:
        sys.exit(f"✗ Secret {name} ({r.status_code}): {r.text[:200]}")


def trigger_workflow(token: str, user: str) -> None:
    r = api("POST",
            f"/repos/{user}/{REPO_NAME}/actions/workflows/goldpilot.yml/dispatches",
            token, json={"ref": "main"})
    if r.status_code == 204:
        print("✓ Primer ciclo disparado — mira la pestaña Actions")
    else:
        print(f"⚠ El disparo manual devolvió {r.status_code} "
              f"(puede que debas activar Actions a mano: repo → Actions → enable)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true", help="crear el repo si no existe")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    user = os.environ.get("GITHUB_USER", "").strip()
    if not token or not user:
        sys.exit("Define GITHUB_TOKEN y GITHUB_USER")

    if args.create:
        create_repo(token, user)

    git_push(token, user)

    for name in REQUIRED_SECRETS:
        value = os.environ.get(name, "").strip()
        if value:
            set_secret(token, user, name, value)
        else:
            print(f"⚠ Secret {name} vacío — recuerda definirlo en Settings→Secrets")

    trigger_workflow(token, user)
    print("\n🎉 LISTO. Tu URL: https://github.com/" + user + "/" + REPO_NAME)
    print("   Vigilancia: Actions → 'GoldPilot FN — vigilancia 24/7'")


if __name__ == "__main__":
    main()
