/** Bearer token is held only in this tab's session storage; no auth cookies are used. */
export function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const token = window.sessionStorage.getItem("tracecore_api_token");
  if (!token) return init ? fetch(input, init) : fetch(input);
  const headers = new Headers(init?.headers);
  headers.set("authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
}
