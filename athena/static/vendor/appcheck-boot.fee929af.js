/* Firebase App Check bootstrap for Pallas Athena.
 *
 * Replaces the former inline script in base.html so the logic survives a
 * strict (nonce-based) CSP and stays cacheable/version-named like every
 * other vendored asset. Configuration is read from the non-executable
 * JSON data block #athena-appcheck-config rendered by base.html.
 *
 * MUST execute after the firebase-app/app-check compat SDKs and BEFORE
 * htmx initializes (document order guarantees this both natively and
 * under Cloudflare Rocket Loader, which preserves script order).
 */
(function () {
  'use strict';

  var el = document.getElementById('athena-appcheck-config');
  if (!el || typeof firebase === 'undefined') return;

  var cfg;
  try {
    cfg = JSON.parse(el.textContent);
  } catch (e) {
    return;
  }
  if (!cfg || !cfg.recaptchaSiteKey) return;

  // Debug token for local development — must be set before activate().
  if (typeof cfg.debugToken !== 'undefined') {
    self.FIREBASE_APPCHECK_DEBUG_TOKEN = cfg.debugToken;
  }

  firebase.initializeApp({
    apiKey: cfg.apiKey,
    authDomain: cfg.projectId + '.firebaseapp.com',
    projectId: cfg.projectId,
    appId: cfg.appId
  });

  var appCheck = firebase.appCheck();
  appCheck.activate(
    new firebase.appCheck.ReCaptchaEnterpriseProvider(cfg.recaptchaSiteKey),
    /* isTokenAutoRefreshEnabled */ true
  );

  // Cache the App Check token and attach it to all HTMX requests.
  var token = '';
  var pending = null;

  function fetchToken() {
    pending = appCheck.getToken(false).then(function (result) {
      token = result.token;
      pending = null;
    }).catch(function () {
      pending = null;
    });
    setTimeout(fetchToken, 50 * 60 * 1000);
    return pending;
  }
  fetchToken();

  // Pause HTMX requests until the first token fetch resolves.
  document.body.addEventListener('htmx:confirm', function (evt) {
    if (token) return;        // token ready — proceed normally
    if (!pending) return;     // no pending fetch — proceed without token
    evt.preventDefault();
    pending.then(function () {
      evt.detail.issueRequest();
    });
  });

  document.body.addEventListener('htmx:configRequest', function (evt) {
    if (token) {
      evt.detail.headers['X-Firebase-AppCheck'] = token;
    }
  });
})();
