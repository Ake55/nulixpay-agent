import { useState } from "react"
import { api, sandboxApprovalUrl } from "./api.js"
import { estimatedTotal, euros, rememberOrder, useCart } from "./cart.jsx"

export function Receipt() {
  const { cart, setCart } = useCart()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const blocked = cart.length === 0 || cart.some((line) => !validQuantity(line))

  function changeQuantity(productId, raw) {
    const quantity = raw === "" ? "" : Number(raw)
    setCart((current) =>
      current.map((line) => (line.product_id === productId ? { ...line, quantity } : line)),
    )
  }

  async function checkout() {
    if (blocked || pending) {
      return
    }
    setPending(true)
    setError("")
    try {
      const order = await api("/payments/create-order", {
        method: "POST",
        body: JSON.stringify({
          items: cart.map((line) => ({
            product_id: line.product_id,
            quantity: line.quantity,
          })),
        }),
      })
      if (order.payment_completed === true || order.payment_status === "PAID") {
        throw new Error("The order was reported as paid before approval. No redirect was made.")
      }
      const approval = sandboxApprovalUrl(order.approval_url)
      if (!order.order_id || !approval) {
        throw new Error("PayPal Sandbox did not return an approval link.")
      }
      rememberOrder(order.order_id)
      window.location.assign(approval)
    } catch (caught) {
      setError(caught.message)
      setPending(false)
    }
  }

  return (
    <aside className="receipt">
      <h2>Your cart</h2>
      {cart.length === 0 ? (
        <p>Your cart is empty. Ask for a product, or add one from the catalog.</p>
      ) : (
        <ul className="lines">
          {cart.map((line) => {
            const short = !validQuantity(line)
            return (
              <li key={line.product_id} className={short ? "short" : undefined}>
                <div className="line-head">
                  <h3>{line.name}</h3>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => setCart((current) => current.filter((item) => item.product_id !== line.product_id))}
                  >
                    Remove
                  </button>
                </div>
                <p>{line.reason}</p>
                {line.matched_features && line.matched_features.length > 0 ? (
                  <p className="quiet">Matches: {line.matched_features.join(", ")}</p>
                ) : null}
                <p className="quiet">{euros(line.unit_price_eur)} each · {line.available_stock} available</p>
                {short ? <p className="warn">Not enough stock for this quantity.</p> : null}
                <label>
                  Quantity
                  <input
                    type="number"
                    min="1"
                    max="99"
                    value={line.quantity}
                    onChange={(event) => changeQuantity(line.product_id, event.target.value)}
                  />
                </label>
              </li>
            )
          })}
        </ul>
      )}
      <p className="total">{cart.length === 0 ? "0.00 EUR" : euros(estimatedTotal(cart))}</p>
      <p className="quiet">Estimated from catalog prices. PayPal is charged the total calculated by the server. This does not charge you yet.</p>
      <button type="button" onClick={checkout} disabled={blocked || pending}>
        {pending ? "Creating the PayPal order…" : "Continue to PayPal Sandbox"}
      </button>
      {error ? <p className="error" role="alert">{error}</p> : null}
    </aside>
  )
}

function validQuantity(line) {
  const quantity = Number(line.quantity)
  return Number.isInteger(quantity) && quantity >= 1 && quantity <= 99 && quantity <= line.available_stock && line.available_stock >= 1
}
