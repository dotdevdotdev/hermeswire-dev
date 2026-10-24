/**
 * Centralized portal API access — bearer-token auth.
 *
 * The portal requires an auth token whenever one is configured (always, for
 * non-loopback binds). The token is entered once per device via the token
 * modal, stored in localStorage, and attached to every request:
 *   - HTTP: `Authorization: Bearer <token>` (use apiFetch everywhere)
 *   - WebSocket: `hermeswire.bearer.<token>` subprotocol (use wsProtocols())
 *
 * On a 401, apiFetch raises the token modal (one shared instance across
 * concurrent failures) and retries after entry.
 */

import { showTokenModal } from './token-modal.js';

const TOKEN_KEY = 'hermeswire_token';
// Pre-rename key (package agentwire -> hermeswire). Migrate the stored token
// forward once so an existing device isn't silently logged out on the next load.
const LEGACY_TOKEN_KEY = 'agentwire_token';

export function getToken() {
    let token = localStorage.getItem(TOKEN_KEY);
    if (token === null) {
        token = localStorage.getItem(LEGACY_TOKEN_KEY);
        if (token) {
            localStorage.setItem(TOKEN_KEY, token);
            localStorage.removeItem(LEGACY_TOKEN_KEY);
        }
    }
    return token;
}

export function setToken(token) {
    localStorage.setItem(TOKEN_KEY, token);
}

/** Subprotocol list for `new WebSocket(url, wsProtocols())`. */
export function wsProtocols() {
    const token = getToken();
    return token ? [`hermeswire.bearer.${token}`] : undefined;
}

let tokenPrompt = null; // concurrent 401s share one modal

export async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    const token = getToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);

    const resp = await fetch(url, { ...options, headers });
    if (resp.status !== 401) return resp;

    // Token missing/stale — prompt once, then retry with the new token.
    if (!tokenPrompt) {
        tokenPrompt = showTokenModal().finally(() => { tokenPrompt = null; });
    }
    await tokenPrompt;
    return apiFetch(url, options);
}
