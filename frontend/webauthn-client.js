/* Browser-side WebAuthn helpers: base64url <-> ArrayBuffer plumbing that the
   credential API needs, kept in one place so the pages stay readable. */

function b64urlToBuf(value) {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes.buffer;
}

function bufToB64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let str = '';
  for (let i = 0; i < bytes.length; i += 1) str += String.fromCharCode(bytes[i]);
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

function biometricsSupported() {
  return typeof window.PublicKeyCredential !== 'undefined' && !!navigator.credentials;
}

async function platformAuthenticatorAvailable() {
  if (!biometricsSupported()) return false;
  try {
    return await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch (err) {
    return false;
  }
}

async function apiPost(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

async function createCredential(options) {
  options.challenge = b64urlToBuf(options.challenge);
  options.user.id = b64urlToBuf(options.user.id);
  (options.excludeCredentials || []).forEach((c) => { c.id = b64urlToBuf(c.id); });

  const credential = await navigator.credentials.create({ publicKey: options });
  return {
    id: credential.id,
    rawId: bufToB64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    transports: credential.response.getTransports ? credential.response.getTransports() : [],
    response: {
      clientDataJSON: bufToB64url(credential.response.clientDataJSON),
      attestationObject: bufToB64url(credential.response.attestationObject),
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

async function getAssertion(options) {
  options.challenge = b64urlToBuf(options.challenge);
  (options.allowCredentials || []).forEach((c) => { c.id = b64urlToBuf(c.id); });

  const assertion = await navigator.credentials.get({ publicKey: options });
  return {
    id: assertion.id,
    rawId: bufToB64url(assertion.rawId),
    type: assertion.type,
    authenticatorAttachment: assertion.authenticatorAttachment,
    response: {
      clientDataJSON: bufToB64url(assertion.response.clientDataJSON),
      authenticatorData: bufToB64url(assertion.response.authenticatorData),
      signature: bufToB64url(assertion.response.signature),
      userHandle: assertion.response.userHandle ? bufToB64url(assertion.response.userHandle) : null,
    },
    clientExtensionResults: assertion.getClientExtensionResults(),
  };
}
