import { useEffect, useRef, type RefObject } from "react"

import { mountDuneAscii } from "./renderDuneAscii"

export function DuneAscii({ surface, animate }: { surface: RefObject<HTMLDivElement | null>; animate: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const still = useRef<HTMLCanvasElement>(null)
  const scene = useRef<ReturnType<typeof mountDuneAscii>>(null)

  useEffect(() => {
    if (!canvas.current || !still.current || !surface.current) return
    const mounted = mountDuneAscii(canvas.current, still.current, surface.current)
    scene.current = mounted
    return () => {
      mounted.dispose()
      scene.current = null
    }
  }, [surface])

  useEffect(() => {
    scene.current?.setEnabled(animate)
  }, [animate])

  return (
    <div className="dune-scene" aria-hidden="true">
      <canvas className="dune-ascii" ref={canvas} />
      <canvas className="dune-ascii dune-ascii--still" ref={still} />
    </div>
  )
}
