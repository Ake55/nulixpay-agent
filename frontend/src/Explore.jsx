import { useState } from "react"
import { api, publicHttpUrl } from "./api.js"

export function Explore() {
  const [query, setQuery] = useState("")
  const [maxPrice, setMaxPrice] = useState("")
  const [result, setResult] = useState(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")

  async function onSearch(event) {
    event.preventDefault()
    const text = query.trim()
    if (!text || pending) {
      return
    }
    setPending(true)
    setError("")
    const params = new URLSearchParams({ query: text, limit: "5" })
    if (maxPrice.trim()) {
      params.set("max_price", maxPrice.trim())
    }
    try {
      const payload = await api(`/products/external/search?${params.toString()}`)
      if (payload.checkout_supported !== false) {
        throw new Error("This response claimed NuLixPay could sell these products. It was ignored.")
      }
      setResult(payload)
    } catch (caught) {
      setResult(null)
      setError(caught.message)
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="explore">
      <h1>Other shops</h1>
      <p className="lede">
        Channel3 finds products sold by other merchants. NuLixPay cannot take payment for them.
      </p>
      <form className="filters" onSubmit={onSearch}>
        <label>
          Search
          <input value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>
        <label>
          Max price
          <input
            inputMode="decimal"
            value={maxPrice}
            onChange={(event) => setMaxPrice(event.target.value)}
          />
        </label>
        <button type="submit" disabled={pending || query.trim() === ""}>
          {pending ? "Searching other shops…" : "Search other shops"}
        </button>
      </form>
      {error ? <p className="error" role="alert">{error}</p> : null}
      {result && result.results.length === 0 ? <p>No merchant products matched that search.</p> : null}
      <ul className="merchant-list">
        {result
          ? result.results.map((product) => (
              <li key={product.channel3_product_id}>
                {publicHttpUrl(product.image_url) ? (
                  <img src={publicHttpUrl(product.image_url)} alt="" />
                ) : (
                  <div className="no-image" aria-hidden="true" />
                )}
                <div>
                  <p className="stamp">{product.label}</p>
                  <h2>{product.name}</h2>
                  {product.brands.length > 0 ? <p className="quiet">{product.brands.join(", ")}</p> : null}
                  {product.description ? <p>{product.description}</p> : null}
                  {product.offers.length === 0 ? <p className="quiet">No merchant offer was included.</p> : null}
                  <ul className="offers">
                    {product.offers.map((offer) => {
                      const href = publicHttpUrl(offer.purchase_url)
                      return (
                        <li key={`${offer.merchant_domain}-${offer.purchase_url}`}>
                          <span>
                            {offer.merchant_domain}: {offer.price} {offer.currency}
                            {offer.availability === "InStock" ? " · in stock" : null}
                            {offer.availability === "OutOfStock" ? " · out of stock" : null}
                          </span>
                          {href ? (
                            <a href={href} target="_blank" rel="noreferrer">
                              View at {offer.merchant_domain}
                            </a>
                          ) : null}
                        </li>
                      )
                    })}
                  </ul>
                </div>
              </li>
            ))
          : null}
      </ul>
    </section>
  )
}
