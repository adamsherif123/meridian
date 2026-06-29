import { createContext } from 'react'
import type { Block, EdgeKind, NodeConfig } from './types'

export interface BoardActions {
  activeBoardId: string | null
  updateNodeTitle: (id: string, title: string) => void
  deleteNode: (id: string) => void
  changeEdgeKind: (id: string, kind: EdgeKind) => void
  changeEdgeLabel: (id: string, label: string) => void
  updateNodeBlocks: (nodeId: string, blocks: Block[]) => void
  updateNodeConfig: (nodeId: string, partial: Partial<NodeConfig>) => void
}

export const BoardContext = createContext<BoardActions | null>(null)
