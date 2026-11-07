import { useState } from "react"
import { api } from "./api.js"
import { euros, lineFromRecommendation, useCart } from "./cart.jsx"

export function Assistant() {
  const { setCart } = useCart()
  const [message, setMessage] = useState("")
  const [thread, setThread] = useState([])
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")

  async function onSubmit(event) {
    event.preventDefault()
    const text = message.trim()
    if (!text || pending) {
      return
    }
    const history = thread.map((entry) => entry.text).slice(-10)
    setMessage("")
    setError("")
    setPending(true)
    setThread((current) => [...current, { role: "you", text }])
    try {
      const reply = await api("/agent/chat", {
        method: "POST",
        body: JSON.stringify({ message: text, history }),
      })
      if (reply.payment_created !== false) {
        throw new Error("The assistant tried to create a payment. That reply was ignored.")
      }
      if (reply.status === "recommendation" && Array.isArray(reply.items) && reply.items.length > 0) {
        setCart(reply.items.map(lineFromRecommendation))
      }
      setThread((current) => [...current, replyEntry(reply)])
    } catch (caught) {
      setError(caught.message)
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="talk">
      <h1>What should we find?</h1>
      <p className="lede">
        Describe a product and a budget. NuLixPay builds a cart from its own catalog. The server sets the price.
      </p>
      <div className="thread" aria-live="polite">
        {thread.length === 0 ? (
          <p className="quiet">Try “noise cancelling headphones and a charger under 200 euros”.</p>
        ) : null}
        {thread.map((entry, index) => (
          <article key={index} className={entry.role === "you" ? "bubble you" : "bubble reply"}>
            <p>{entry.text}</p>
            {entry.budget ? <p className="quiet">Budget used: {euros(entry.budget)}</p> : null}
            {entry.unmet && entry.unmet.length > 0 ? (
              <ul>
                {entry.unmet.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            ) : null}
          </article>
        ))}
      </div>
      <form className="composer" onSubmit={onSubmit}>
        <label htmlFor="request">Shopping request</label>
        <textarea
          id="request"
          rows={3}
          maxLength={2000}
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="Noise cancelling headphones and a charger under 200 euros"
        />
        <button type="submit" disabled={pending || message.trim() === ""}>
          {pending ? "Looking through the catalog…" : "Ask NuLixPay"}
        </button>
      </form>
      {error ? <p className="error" role="alert">{error}</p> : null}
    </section>
  )
}

function replyEntry(reply) {
  const unmet = Array.isArray(reply.unmet_requirements) ? reply.unmet_requirements : []
  if (reply.status === "clarification") {
    return {
      role: "assistant",
      text: reply.clarifying_question || reply.explanation,
      unmet: [],
      budget: null,
    }
  }
  if (reply.status === "no_match" || !reply.items || reply.items.length === 0) {
    return {
      role: "assistant",
      text: reply.explanation || "Nothing in the catalog fits that request.",
      unmet,
      budget: reply.understood_budget_eur,
    }
  }
  return {
    role: "assistant",
    text: reply.explanation,
    unmet,
    budget: reply.understood_budget_eur,
  }
}
