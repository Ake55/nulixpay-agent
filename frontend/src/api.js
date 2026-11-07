// The browser talks only to the local API. PayPal, Channel3, and model keys
// stay in the backend .env file and are never read here.

const API_BASE = "http://127.0.0.1:8000"

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

export async function api(path, options = {}) {
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...options.headers,
      },
    })
  } catch {
    throw new ApiError("The NuLixPay API is not running. Start it on port 8000 and try again.", 0)
  }
  if (!response.ok) {
    throw new ApiError(await errorMessage(response), response.status)
  }
  return response.json()
}

async function errorMessage(response) {
  try {
    const payload = await response.json()
    const detail = payload && payload.detail
    if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.message) {
      return detail.message
    }
    if (typeof detail === "string" && detail.trim()) {
      return detail
    }
    if (Array.isArray(detail) && detail[0] && typeof detail[0].msg === "string") {
      return detail[0].msg
    }
  } catch {
    // The body was not JSON.
  }
  return `The request failed (${response.status}).`
}

export function sandboxApprovalUrl(value) {
  try {
    const url = new URL(value)
    const allowed = url.protocol === "https:" && (url.hostname === "www.sandbox.paypal.com" || url.hostname === "sandbox.paypal.com")
    return allowed ? url.href : null
  } catch {
    return null
  }
}

export function publicHttpUrl(value) {
  try {
    const url = new URL(value)
    if (url.protocol !== "https:" && url.protocol !== "http:") {
      return null
    }
    return url.href
  } catch {
    return null
  }
}
