/**
 * @name URL redirection from remote source (project-sanitized)
 * @description Redirecting to a user-controlled URL can allow phishing attacks.
 *              This is a copy of the standard py/url-redirection query that
 *              additionally treats security.safe_internal_redirect() as a
 *              validation barrier.
 * @kind path-problem
 * @problem.severity error
 * @security-severity 6.1
 * @precision high
 * @id py/url-redirection-custom
 * @tags security
 *       external/cwe/cwe-601
 */

import python
import semmle.python.security.dataflow.UrlRedirectQuery
// REQUIRED: places SafeInternalRedirectSanitizer in this query's import closure
// so the abstract-class extent picks it up (the stock query would not).
import UrlRedirectSanitizers
import UrlRedirectFlow::PathGraph

from UrlRedirectFlow::PathNode source, UrlRedirectFlow::PathNode sink
where UrlRedirectFlow::flowPath(source, sink)
select sink.getNode(), source, sink, "Untrusted URL redirection depends on a $@.",
  source.getNode(), "user-provided value"
