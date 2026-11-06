import { Pause, Play } from "lucide-react"
import { useId, useRef, useState, useSyncExternalStore, type ReactNode } from "react"

import { DuneAscii } from "./DuneAscii"
import "./dune-home.css"

const motionPreference = "(prefers-reduced-motion: reduce)"

function subscribeToMotionPreference(onChange: () => void) {
  const query = window.matchMedia(motionPreference)
  query.addEventListener("change", onChange)
  return () => query.removeEventListener("change", onChange)
}

const prefersReducedMotion = () => window.matchMedia(motionPreference).matches

type DuneHomeProps = {
  eyebrow: string
  tagline: string
  description: string
  footerLabel: string
  footerNote: string
  children: ReactNode
}

export function DuneHome({ eyebrow, tagline, description, footerLabel, footerNote, children }: DuneHomeProps) {
  const surface = useRef<HTMLDivElement>(null)
  const titleId = useId()
  const [paused, setPaused] = useState(false)
  const reducedMotion = useSyncExternalStore(subscribeToMotionPreference, prefersReducedMotion)
  const animate = !reducedMotion && !paused

  return (
    <div className="dune-home" data-animate={animate} ref={surface}>
      <DuneAscii surface={surface} animate={animate} />

      <header className="dune-header">
        <div className="dune-signature">
          <span className="dune-eclipse" aria-hidden="true" />
          <span>DUNE</span>
        </div>
        {!reducedMotion && (
          <button className="dune-motion" type="button" onClick={() => setPaused(value => !value)}>
            {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
            {paused ? "Resume effects" : "Pause effects"}
          </button>
        )}
      </header>

      <div className="dune-main">
        <section className="dune-intro" aria-labelledby={titleId}>
          <p className="dune-eyebrow">{eyebrow}</p>
          <h1 id={titleId}>
            <span className="sr-only">DUNE</span>
            <svg className="dune-wordmark" viewBox="0 0 384 70" fill="none" stroke="currentColor" strokeWidth="5" aria-hidden="true">
              <path d="M3 67V3h39a32 32 0 0 1 0 64H3Zm104-64v31a33 33 0 0 0 66 0V3m35 64V3l67 64V3m36 0h72m-72 32h56m-56 32h72" />
            </svg>
          </h1>
          <p className="dune-tagline">{tagline}</p>
          <p className="dune-description">{description}</p>
        </section>

        {children}
      </div>

      <footer className="dune-footer">
        <span>{footerLabel}</span>
        <span className="dune-interaction" aria-hidden="true">
          <span>Move the cursor to stir the sand</span>
          <span>Tap to stir the sand</span>
        </span>
        <span>{footerNote}</span>
      </footer>
    </div>
  )
}
