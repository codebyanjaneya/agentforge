// Shared helpers for the M1-G tests (not a test file: it does not match tests/*.test.mjs).

import { spawnSync } from "node:child_process";
import crypto from "node:crypto";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const BACKEND = path.resolve(here, "..", "..", "backend");

export function newSigningKey() {
  const { privateKey, publicKey } = crypto.generateKeyPairSync("ec", { namedCurve: "P-256" });
  return {
    privatePem: privateKey.export({ type: "pkcs8", format: "pem" }),
    publicPem: publicKey.export({ type: "spki", format: "pem" }),
  };
}

export function envWith(privatePem, extra = {}) {
  return { BACKEND_TOKEN_PRIVATE_KEY: privatePem, BACKEND_TOKEN_KID: "test-kid-1", ...extra };
}

export function decodePart(token, index) {
  return JSON.parse(Buffer.from(token.split(".")[index], "base64url").toString("utf8"));
}

const PY = [
  path.join(BACKEND, "venv", "Scripts", "python.exe"),
  path.join(BACKEND, "venv", "bin", "python"),
].find(existsSync);

export const backendPythonAvailable = Boolean(PY);

// Runs the REAL backend verifier (backend/auth/tokens.py, M1-D) on a token. Returns {claims} or {error: reason}.
export function backendVerify(token, publicPem, kid = "test-kid-1", { issuer = "agentforge-web", audience = "agentforge-api" } = {}) {
  const code = [
    "import json, sys",
    "from auth.errors import AuthenticationError",
    "from auth.tokens import AuthConfig, verify_access_token",
    "data = json.load(sys.stdin)",
    "config = AuthConfig.from_pem_keys(data['issuer'], data['audience'], {data['kid']: data['pem']})",
    "try:",
    "    c = verify_access_token(data['token'], config)",
    "    print(json.dumps({'claims': {'provider': c.provider, 'subject': c.subject, 'email': c.email, 'jti': c.jti,",
    "                                 'issued_at': c.issued_at, 'expires_at': c.expires_at}}))",
    "except AuthenticationError as exc:",
    "    print(json.dumps({'error': exc.reason}))",
  ].join("\n");
  const result = spawnSync(PY, ["-c", code], {
    input: JSON.stringify({ token, pem: publicPem, kid, issuer, audience }),
    env: { ...process.env, PYTHONPATH: BACKEND, PYTHON_DOTENV_DISABLED: "1", PYTHONDONTWRITEBYTECODE: "1" },
    encoding: "utf8",
    timeout: 60000,
  });
  if (result.status !== 0) throw new Error(`backend verifier crashed: ${result.stderr.slice(0, 400)}`);
  return JSON.parse(result.stdout.trim().split("\n").pop());
}
