/**
 * Models the project helper `security.safe_internal_redirect(target, fallback)`
 * as a barrier for the URL-redirection taint-tracking query (CWE-601).
 *
 * The function returns `target` only when it is a validated same-origin
 * internal path, otherwise the `fallback`; its return value is therefore a
 * safe/validated value. Marking that return value as a `UrlRedirect::Sanitizer`
 * cuts exactly the taint path from the user-controlled `return_to`/`next`
 * value to the `redirect()` sink, while leaving genuinely unsanitized
 * redirects flagged.
 */

import python
// Re-exports the abstract classes `UrlRedirect::Sanitizer` and
// `UrlRedirect::FlowState` (via `import UrlRedirectCustomizations::UrlRedirect as UrlRedirect`).
import semmle.python.security.dataflow.UrlRedirectQuery

/** The return value of any call to `safe_internal_redirect(...)`, any import style. */
class SafeInternalRedirectSanitizer extends UrlRedirect::Sanitizer {
  SafeInternalRedirectSanitizer() {
    exists(Call call |
      this.asExpr() = call and
      (
        // `from security import safe_internal_redirect` -> bare-name call (actual usage)
        call.getFunc().(Name).getId() = "safe_internal_redirect"
        or
        // `security.safe_internal_redirect(...)` -> attribute call (defensive)
        call.getFunc().(Attribute).getName() = "safe_internal_redirect"
      )
    )
  }

  override predicate sanitizes(UrlRedirect::FlowState state) { any() }
}
