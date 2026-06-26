import { createContext } from 'react'
import type { EdgeKind } from './types'

export interface BoardActions {
  updateNodeTitle: (id: string, title: string) => void
  deleteNode: (id: string) => void
  changeEdgeKind: (id: string, kind: EdgeKind) => void
}

export const BoardContext = createContext<BoardActions | null>(null)
