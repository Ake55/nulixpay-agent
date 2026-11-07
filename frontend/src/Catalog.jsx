import { useEffect, useState } from "react"
import { api } from "./api.js"
import { euros, lineFromCatalog, useCart } from "./cart.jsx"

export function Catalog() {
  const { cart, setCart } = useCart()
  const [query, setQuery] = useState("")
  const [maxPrice, setMaxPrice] = useState("")
  const [products, setProducts] = useState([])
  const [pending, setPending] = useState(true)
  const [error, setError] = useState("")

  useEffect(() => {
    let active = true
    api("/products")
      .then((rows) => {
        if (active) {
          setProducts(rows)
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
  }, [])

  async function onSearch(event) {
    event.preventDefault()
    setPending(true)
    setError("")
    const params = new URLSearchParams()
    if (query.trim()) {
      params.set("query", query.trim())
    }
    if (maxPrice.trim()) {
      params.set("max_price", maxPrice.trim())
    }
    try {
      const rows = await api(`/products/search?${params.toString()}`)
      setProducts(rows)
    } catch (caught) {
      setError(caught.message)
    } finally {
      setPending(false)
    }
  }

  function add(product) {
    if (product.stock < 1) {
      return
    }
    setCart((current) => {
      const existing = current.find((line) => line.product_id === product.id)
      if (!existing) {
        return [...current, lineFromCatalog(product)]
      }
      const quantity = Math.min(existing.quantity + 1, existing.available_stock, 99)
      return current.map((line) => (line.product_id === product.id ? { ...line, quantity } : line))
    })
  }

  return (
    <section className="talk">
      <h1>NuLixPay catalog</h1>
      <p className="lede">These are the products this shop can sell. Prices come from the catalog file.</p>
      <form className="filters" onSubmit={onSearch}>
        <label>
          Search
          <input value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>
        <label>
          Max price (EUR)
          <input
            inputMode="decimal"
            value={maxPrice}
            onChange={(event) => setMaxPrice(event.target.value)}
          />
        </label>
        <button type="submit" disabled={pending}>Search catalog</button>
      </form>
      {error ? <p className="error" role="alert">{error}</p> : null}
      {pending ? <p className="quiet">Loading the catalog…</p> : null}
      {!pending && products.length === 0 ? <p>Nothing in the catalog matches that search.</p> : null}
      <ul className="product-list">
        {products.map((product) => {
          const inCart = cart.some((line) => line.product_id === product.id)
          return (
            <li key={product.id}>
              <div>
                <h2>{product.name}</h2>
                <p>{product.description}</p>
                <p className="quiet">
                  {product.category} · {euros(product.price_eur)} · {product.stock} in stock
                </p>
                <p className="quiet">{product.features.join(", ")}</p>
              </div>
              {product.stock < 1 ? (
                <p className="warn">No available stock</p>
              ) : (
                <button type="button" onClick={() => add(product)}>
                  {inCart ? "Add another" : "Add to cart"}
                </button>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
