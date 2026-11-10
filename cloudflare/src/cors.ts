const ALLOWED = new Set([
  "https://workbench.minocc.top",
  "https://minocc.top",
  "https://www.minocc.top",
  "https://mino-workbench.pages.dev",
]);

export function corsHeaders(request: Request): Headers {
  const headers = new Headers();
  const origin = request.headers.get("Origin") || "";
  if (ALLOWED.has(origin)) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Access-Control-Allow-Credentials", "true");
    headers.set("Vary", "Origin");
  }
  headers.set(
    "Access-Control-Allow-Headers",
    request.headers.get("Access-Control-Request-Headers") ||
      "Authorization, Content-Type",
  );
  headers.set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS");
  return headers;
}

export function withCors(request: Request, response: Response): Response {
  const headers = new Headers(response.headers);
  const extra = corsHeaders(request);
  extra.forEach((value, key) => headers.set(key, value));
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export function preflight(request: Request): Response {
  return new Response(null, { status: 204, headers: corsHeaders(request) });
}
