import { CartProvider } from "./cart.jsx"
import { Assistant } from "./Assistant.jsx"
import { Catalog } from "./Catalog.jsx"
import { Explore } from "./Explore.jsx"
import { Receipt } from "./Receipt.jsx"
import { ReturnView } from "./ReturnView.jsx"

export function App() {
  const path = window.location.pathname
  let page = "assistant"
  if (path.startsWith("/catalog")) {
    page = "catalog"
  } else if (path.startsWith("/explore")) {
    page = "explore"
  } else if (path.startsWith("/return")) {
    page = "return"
  }

  return (
    <CartProvider>
      <div className="shell">
        <header className="top">
          <a className="mark" href="/">NuLixPay</a>
          <nav className="nav">
            <a href="/" aria-current={page === "assistant" ? "page" : undefined}>Assistant</a>
            <a href="/catalog" aria-current={page === "catalog" ? "page" : undefined}>Catalog</a>
            <a href="/explore" aria-current={page === "explore" ? "page" : undefined}>Other shops</a>
          </nav>
        </header>
        {page === "return" ? <ReturnView /> : null}
        {page === "explore" ? <Explore /> : null}
        {page === "assistant" || page === "catalog" ? (
          <div className="desk">
            {page === "assistant" ? <Assistant /> : <Catalog />}
            <Receipt />
          </div>
        ) : null}
      </div>
    </CartProvider>
  )
}
