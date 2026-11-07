import { createContext, useContext, useEffect, useState } from "react"

const CART_KEY = "nulixpay-cart"
const ORDER_KEY = "nulixpay-order-id"

const CartContext = createContext(null)

function readCart() {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(CART_KEY) || "[]")
    if (!Array.isArray(parsed)) {
      return []
    }
    return parsed.filter((line) => line && typeof line.product_id === "string" && typeof line.name === "string")
  } catch {
    return []
  }
}

export function CartProvider({ children }) {
  const [cart, setCart] = useState(readCart)
  useEffect(() => {
    sessionStorage.setItem(CART_KEY, JSON.stringify(cart))
  }, [cart])
  return <CartContext.Provider value={{ cart, setCart }}>{children}</CartContext.Provider>
}

export function useCart() {
  const value = useContext(CartContext)
  if (!value) {
    throw new Error("useCart must be used inside CartProvider")
  }
  return value
}

export function rememberOrder(orderId) {
  sessionStorage.setItem(ORDER_KEY, orderId)
}

export function rememberedOrder() {
  return sessionStorage.getItem(ORDER_KEY) || ""
}

export function euros(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) {
    return "—"
  }
  return `${number.toFixed(2)} EUR`
}

export function estimatedTotal(cart) {
  const cents = cart.reduce((sum, line) => {
    const unit = Number(line.unit_price_eur)
    const quantity = Number(line.quantity)
    if (!Number.isFinite(unit) || !Number.isInteger(quantity)) {
      return sum
    }
    return sum + Math.round(unit * 100) * quantity
  }, 0)
  return (cents / 100).toFixed(2)
}

export function lineFromCatalog(product) {
  return {
    product_id: product.id,
    name: product.name,
    category: product.category,
    quantity: 1,
    unit_price_eur: Number(product.price_eur).toFixed(2),
    available_stock: product.stock,
    matched_features: product.features,
    reason: product.description,
  }
}

export function lineFromRecommendation(item) {
  return {
    product_id: item.product_id,
    name: item.name,
    category: item.category,
    quantity: item.quantity,
    unit_price_eur: item.unit_price_eur,
    available_stock: item.available_stock,
    matched_features: item.matched_features,
    reason: item.reason,
  }
}
