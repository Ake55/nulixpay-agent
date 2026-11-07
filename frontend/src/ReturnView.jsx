import { useEffect, useState } from "react"
import { api, sandboxApprovalUrl } from "./api.js"
import { euros, rememberedOrder } from "./cart.jsx"

export function ReturnView() {
  const params = new URLSearchParams(window.location.search)
  const cancelled = params.get("cancelled") === "1"
  const token = validOrderId(params.get("token")) || validOrderId(rememberedOrder())
  const [order, setOrder] = useState(null)
  const [pending, setPending] = useState(Boolean(token))
  const [capturing, setCapturing] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    if (!token) {
      return undefined
    }
    let active = true
    api(`/payments/${token}`)
      .then((payload) => {
        if (active) {
          setOrder(payload)
        }
      })
      .catch((caught) => {
        if (active) {
          setError(caught.message)
        }
      })
      .finally(() => {
        if (active) {
          setPending(false)
        }
      })
    return () => {
      active = false
    }
  }, [token])

  async function capture() {
    if (!token || capturing) {
      return
    }
    setCapturing(true)
    setError("")
    try {
      const payload = await api(`/payments/${token}/capture`, {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      })
      setOrder(payload)
    } catch (caught) {
      setError(caught.message)
    } finally {
      setCapturing(false)
    }
  }

  const paid = order && order.payment_status === "PAID" && order.payment_completed === true
  const approved = order && order.order_status === "APPROVED" && !paid
  const approval = order ? sandboxApprovalUrl(order.approval_url) : null

  return (
    <section className="return">
      <h1>{paid ? "Payment completed" : "PayPal Sandbox"}</h1>
      {cancelled ? <p className="warn">PayPal approval was cancelled. No payment was captured.</p> : null}
      {!token ? <p>There is no PayPal order to check. Start from the cart.</p> : null}
      {pending ? <p>Checking this order with PayPal Sandbox…</p> : null}
      {order && paid ? (
        <div className="success">
          <p>The backend confirmed this payment.</p>
          <p className="total">{euros(order.amount)}</p>
          <p className="quiet">Order {order.order_id}</p>
        </div>
      ) : null}
      {order && !paid ? (
        <div>
          <p>{order.message}</p>
          <p className="total">{euros(order.amount)}</p>
          <p className="quiet">
            Payment status: {order.payment_status}. PayPal status: {order.order_status}.
          </p>
          <ul className="lines">
            {(order.items || []).map((item) => (
              <li key={item.product_id}>
                <h3>{item.name}</h3>
                <p className="quiet">
                  {item.quantity} × {euros(item.unit_price)}
                </p>
              </li>
            ))}
          </ul>
          {approved ? (
            <button type="button" onClick={capture} disabled={capturing}>
              {capturing ? "Capturing the payment…" : "Capture payment"}
            </button>
          ) : null}
          {!approved && approval ? (
            <p>
              <a href={approval}>Continue to PayPal Sandbox</a>
            </p>
          ) : null}
        </div>
      ) : null}
      {error ? <p className="error" role="alert">{error}</p> : null}
    </section>
  )
}

function validOrderId(value) {
  return typeof value === "string" && /^[A-Z0-9]{1,36}$/.test(value) ? value : ""
}
