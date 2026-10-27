// The intake site's pages: a second entry of the console, built into intake/public, which the
// intake Worker serves. It shares the console's case views and theme, and none of its clients
// of edge (eslint.config.js keeps it so).
import { RouterProvider } from "@tanstack/react-router"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import "@/intake/index.css"
import { router } from "@/intake/router"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>
)
