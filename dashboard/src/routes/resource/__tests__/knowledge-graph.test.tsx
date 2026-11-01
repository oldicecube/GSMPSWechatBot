import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeGraphPage } from '../knowledge-graph'
import * as memoryApi from '@/lib/memory-api'

const navigateMock = vi.fn()
const toastMock = vi.fn()

vi.mock('@tanstack/react-router', () => ({
  useNavigate: () => navigateMock,
}))

vi.mock('@/hooks/use-toast', () => ({
  useToast: () => ({ toast: toastMock }),
}))

vi.mock('@/components/memory/MemoryDeleteDialog', () => ({
  MemoryDeleteDialog: ({
    open,
    onExecute,
    preview,
  }: {
    open: boolean
    onExecute?: () => void
    preview?: { mode?: string; item_count?: number } | null
  }) => (
    open ? (
      <div data-testid="memory-delete-dialog">
        <div>{`delete:${preview?.mode ?? 'none'}:${preview?.item_count ?? 0}`}</div>
        <button type="button" onClick={onExecute}>执行删除</button>
      </div>
    ) : null
  ),
}))

vi.mock('../knowledge-graph/GraphVisualization', () => ({
  GraphVisualization: ({
    graphData,
    onNodeClick,
    onEdgeClick,
  }: {
    graphData: { nodes: Array<{ id: string }>; edges: Array<{ source: string; target: string }> }
    onNodeClick: (event: React.MouseEvent, node: { id: string }) => void
    onEdgeClick: (event: React.MouseEvent, edge: { source: string; target: string }) => void
  }) => (
    <div data-testid="graph-visualization">
      <div>{`nodes:${graphData.nodes.length},edges:${graphData.edges.length}`}</div>
      {graphData.nodes[0] ? (
        <button type="button" onClick={(event) => onNodeClick(event as never, { id: graphData.nodes[0].id })}>
          选择节点
        </button>
      ) : null}
      {graphData.edges[0] ? (
        <button
          type="button"
          onClick={(event) =>
            onEdgeClick(event as never, {
              source: graphData.edges[0].source,
              target: graphData.edges[0].target,
            })}
        >
          选择边
        </button>
      ) : null}
    </div>
  ),
}))

vi.mock('../knowledge-graph/GraphDialogs', () => ({
  NodeDetailDialog: ({
    selectedNodeData,
    nodeDetail,
    onOpenEvidence,
    onDeleteEntity,
  }: {
    selectedNodeData: { id: string } | null
    nodeDetail: { relations?: Array<{ predicate: string }>; paragraphs?: Array<unknown> } | null
    onOpenEvidence?: () => void
    onDeleteEntity?: (options: { includeParagraphs: boolean }) => void
  }) => (
    selectedNodeData ? (
      <div data-testid="node-detail-dialog">
        <div>{`node:${selectedNodeData.id}`}</div>
        <div>{`relations:${nodeDetail?.relations?.[0]?.predicate ?? 'none'}`}</div>
        <div>{`paragraphs:${nodeDetail?.paragraphs?.length ?? 0}`}</div>
        <button type="button" onClick={onOpenEvidence}>切到证据视图</button>
        <button type="button" onClick={() => onDeleteEntity?.({ includeParagraphs: true })}>删除实体</button>
      </div>
    ) : null
  ),
  EdgeDetailDialog: ({
    selectedEdgeData,
    edgeDetail,
    onOpenEvidence,
  }: {
    selectedEdgeData: { source: { id: string }; target: { id: string } } | null
    edgeDetail: { edge?: { predicates?: string[] }; paragraphs?: Array<unknown> } | null
    onOpenEvidence?: () => void
  }) => (
    selectedEdgeData ? (
      <div data-testid="edge-detail-dialog">
        <div>{`edge:${selectedEdgeData.source.id}->${selectedEdgeData.target.id}`}</div>
        <div>{`predicates:${edgeDetail?.edge?.predicates?.join(',') ?? 'none'}`}</div>
        <div>{`paragraphs:${edgeDetail?.paragraphs?.length ?? 0}`}</div>
        <button type="button" onClick={onOpenEvidence}>切到证据视图</button>
      </div>
    ) : null
  ),
  RelationDetailDialog: () => null,
  ParagraphDetailDialog: ({
    paragraph,
    onDeleteParagraph,
  }: {
    paragraph: { hash: string } | null
    onDeleteParagraph?: (paragraph: { hash: string }) => void
  }) => (
    paragraph ? (
      <div data-testid="paragraph-detail-dialog">
        <div>{`paragraph:${paragraph.hash}`}</div>
        <button type="button" onClick={() => onDeleteParagraph?.(paragraph)}>删除这段证据</button>
      </div>
    ) : null
  ),
}))

vi.mock('@/lib/memory-api', () => ({
  getMemoryGraph: vi.fn(),
  getMemoryGraphSearch: vi.fn(),
  getMemoryGraphNodeDetail: vi.fn(),
  getMemoryGraphEdgeDetail: vi.fn(),
  getMemoryGraphParagraphDetail: vi.fn(),
  previewMemoryDelete: vi.fn(),
  executeMemoryDelete: vi.fn(),
  restoreMemoryDelete: vi.fn(),
}))

describe('KnowledgeGraphPage', () => {
  beforeEach(() => {
    navigateMock.mockReset()
    toastMock.mockReset()
    vi.mocked(memoryApi.getMemoryGraph).mockResolvedValue({
      success: true,
      nodes: [
        { id: 'alpha', name: 'Alpha' },
        { id: 'beta', name: 'Beta' },
      ],
      edges: [
        {
          source: 'alpha',
          target: 'beta',
          weight: 1,
          predicates: ['关联'],
          relation_count: 1,
          evidence_count: 2,
          relation_hashes: ['rel-1'],
          label: '关联',
        },
      ],
      total_nodes: 2,
      total_edges: 1,
    })
    vi.mocked(memoryApi.getMemoryGraphSearch).mockResolvedValue({
      success: true,
      query: 'alpha',
      limit: 50,
      count: 0,
      items: [],
    })
    vi.mocked(memoryApi.getMemoryGraphNodeDetail).mockResolvedValue({
      success: true,
      node: { id: 'alpha', type: 'entity', content: 'Alpha', hash: 'entity-1', appearance_count: 3 },
      relations: [
        {
          hash: 'rel-1',
          subject: 'alpha',
          predicate: '关联',
          object: 'beta',
          text: 'alpha 关联 beta',
          confidence: 0.9,
          paragraph_count: 1,
          paragraph_hashes: ['p-1'],
          source_paragraph: 'p-1',
        },
      ],
      paragraphs: [
        {
          hash: 'p-1',
          content: 'Alpha 提到了 Beta',
          preview: 'Alpha 提到了 Beta',
          source: 'demo',
          entity_count: 2,
          relation_count: 1,
          entities: ['Alpha', 'Beta'],
          relations: ['alpha 关联 beta'],
        },
      ],
      evidence_graph: {
        nodes: [
          { id: 'entity:alpha', type: 'entity', content: 'Alpha' },
          { id: 'relation:rel-1', type: 'relation', content: 'alpha 关联 beta' },
          { id: 'paragraph:p-1', type: 'paragraph', content: 'Alpha 提到了 Beta' },
        ],
        edges: [
          { source: 'paragraph:p-1', target: 'entity:alpha', kind: 'mentions', label: '提及', weight: 1 },
          { source: 'paragraph:p-1', target: 'relation:rel-1', kind: 'supports', label: '支撑', weight: 1 },
        ],
        focus_entities: ['alpha'],
      },
    })
    vi.mocked(memoryApi.getMemoryGraphEdgeDetail).mockResolvedValue({
      success: true,
      edge: {
        source: 'alpha',
        target: 'beta',
        weight: 1,
        predicates: ['关联'],
        relation_count: 1,
        evidence_count: 1,
        relation_hashes: ['rel-1'],
        label: '关联',
      },
      relations: [
        {
          hash: 'rel-1',
          subject: 'alpha',
          predicate: '关联',
          object: 'beta',
          text: 'alpha 关联 beta',
          confidence: 0.9,
          paragraph_count: 1,
          paragraph_hashes: ['p-1'],
          source_paragraph: 'p-1',
        },
      ],
      paragraphs: [
        {
          hash: 'p-1',
          content: 'Alpha 提到了 Beta',
          preview: 'Alpha 提到了 Beta',
          source: 'demo',
          entity_count: 2,
          relation_count: 1,
          entities: ['Alpha', 'Beta'],
          relations: ['alpha 关联 beta'],
        },
      ],
      evidence_graph: {
        nodes: [
          { id: 'entity:alpha', type: 'entity', content: 'Alpha' },
          { id: 'entity:beta', type: 'entity', content: 'Beta' },
          { id: 'relation:rel-1', type: 'relation', content: 'alpha 关联 beta' },
        ],
        edges: [
          { source: 'relation:rel-1', target: 'entity:alpha', kind: 'subject', label: '主语', weight: 1 },
          { source: 'relation:rel-1', target: 'entity:beta', kind: 'object', label: '宾语', weight: 1 },
        ],
        focus_entities: ['alpha', 'beta'],
      },
    })
    vi.mocked(memoryApi.getMemoryGraphParagraphDetail).mockResolvedValue({
      success: true,
      paragraph: {
        hash: 'p-1',
        content: 'Alpha 提到了 Beta',
        preview: 'Alpha 提到了 Beta',
        source: 'demo',
        entity_count: 2,
        relation_count: 1,
        entities: ['Alpha', 'Beta'],
        relations: ['alpha 关联 beta'],
      },
      evidence_graph: {
        nodes: [
          { id: 'paragraph:p-1', type: 'paragraph', content: 'Alpha 提到了 Beta', metadata: { hash: 'p-1' } },
          { id: 'entity:alpha', type: 'entity', content: 'Alpha' },
          { id: 'relation:rel-1', type: 'relation', content: 'alpha 关联 beta' },
        ],
        edges: [
          { source: 'paragraph:p-1', target: 'entity:alpha', kind: 'mentions', label: '提及', weight: 1 },
          { source: 'paragraph:p-1', target: 'relation:rel-1', kind: 'supports', label: '支撑', weight: 1 },
        ],
        focus_entities: ['Alpha', 'Beta'],
      },
    })
    vi.mocked(memoryApi.previewMemoryDelete).mockResolvedValue({
      success: true,
      mode: 'mixed',
      selector: { entity_hashes: ['entity-1'] },
      counts: { entities: 1, relations: 1, paragraphs: 1 },
      sources: ['demo'],
      items: [{ item_type: 'entity', item_hash: 'entity-1', label: 'Alpha' }],
      item_count: 1,
      dry_run: true,
    } as never)
    vi.mocked(memoryApi.executeMemoryDelete).mockResolvedValue({
      success: true,
      mode: 'mixed',
      operation_id: 'del-1',
      counts: { entities: 1, relations: 1, paragraphs: 1 },
      sources: ['demo'],
      deleted_count: 3,
      deleted_entity_count: 1,
      deleted_relation_count: 1,
      deleted_paragraph_count: 1,
      deleted_source_count: 0,
    } as never)
    vi.mocked(memoryApi.restoreMemoryDelete).mockResolvedValue({ success: true } as never)
  })

  it('calls backend graph search and renders no-hit state', async () => {
    const user = userEvent.setup()

    render(<KnowledgeGraphPage />)

    expect(await screen.findByText('长期记忆图谱')).toBeInTheDocument()
    expect(screen.getByText(/总节点 2/)).toBeInTheDocument()
    expect(screen.getByTestId('graph-visualization')).toHaveTextContent('nodes:2,edges:1')

    await user.type(screen.getByPlaceholderText('搜索实体、关系、hash（后端全库）'), 'missing')
    expect(memoryApi.getMemoryGraph).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '搜索' }))

    await waitFor(() => {
      expect(memoryApi.getMemoryGraphSearch).toHaveBeenCalledWith('missing', 50)
    })
    expect(await screen.findByText('未命中实体或关系。')).toBeInTheDocument()
  })

  it('supports clicking entity search result to locate evidence', async () => {
    const user = userEvent.setup()
    vi.mocked(memoryApi.getMemoryGraphSearch).mockResolvedValue({
      success: true,
      query: 'alpha',
      limit: 50,
      count: 1,
      items: [
        {
          type: 'entity',
          title: 'Alpha',
          matched_field: 'name',
          matched_value: 'Alpha',
          entity_name: 'alpha',
          entity_hash: 'entity-1',
          appearance_count: 3,
        },
      ],
    })

    render(<KnowledgeGraphPage />)

    await screen.findByTestId('graph-visualization')
    await user.type(screen.getByPlaceholderText('搜索实体、关系、hash（后端全库）'), 'alpha')
    await user.click(screen.getByRole('button', { name: '搜索' }))

    await screen.findByText('搜索词：alpha')
    await user.click(screen.getByRole('button', { name: /Alpha/ }))

    await waitFor(() => {
      expect(memoryApi.getMemoryGraphNodeDetail).toHaveBeenCalledWith('alpha')
    })
    expect(screen.getByRole('tab', { name: '证据视图' })).toHaveAttribute('data-state', 'active')
  })

  it('supports clicking relation search result to locate evidence', async () => {
    const user = userEvent.setup()
    vi.mocked(memoryApi.getMemoryGraphSearch).mockResolvedValue({
      success: true,
      query: '关联',
      limit: 50,
      count: 1,
      items: [
        {
          type: 'relation',
          title: 'alpha 关联 beta',
          matched_field: 'predicate',
          matched_value: '关联',
          subject: 'alpha',
          predicate: '关联',
          object: 'beta',
          relation_hash: 'rel-1',
          confidence: 0.9,
        },
      ],
    })

    render(<KnowledgeGraphPage />)

    await screen.findByTestId('graph-visualization')
    await user.type(screen.getByPlaceholderText('搜索实体、关系、hash（后端全库）'), '关联')
    await user.click(screen.getByRole('button', { name: '搜索' }))
    await user.click(screen.getByRole('button', { name: /alpha 关联 beta/ }))

    await waitFor(() => {
      expect(memoryApi.getMemoryGraphEdgeDetail).toHaveBeenCalledWith('alpha', 'beta')
    })
    expect(screen.getByRole('tab', { name: '证据视图' })).toHaveAttribute('data-state', 'active')
  })

  it('falls back to local filtering when backend search fails', async () => {
    const user = userEvent.setup()
    vi.mocked(memoryApi.getMemoryGraphSearch).mockRejectedValue(new Error('search unavailable'))

    render(<KnowledgeGraphPage />)

    await screen.findByTestId('graph-visualization')
    await user.type(screen.getByPlaceholderText('搜索实体、关系、hash（后端全库）'), 'missing')
    await user.click(screen.getByRole('button', { name: '搜索' }))

    expect(await screen.findByText('还没有可展示的长期记忆图谱')).toBeInTheDocument()
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '后端检索失败，已回退本地筛选',
      }),
    )
  })

  it('shows empty state when switching to evidence view without a selection', async () => {
    const user = userEvent.setup()

    render(<KnowledgeGraphPage />)

    expect(await screen.findByTestId('graph-visualization')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: '证据视图' }))

    expect(await screen.findByText('证据视图还没有可展示的选择')).toBeInTheDocument()
  })

  it('closes node dialog when switching to evidence view and renders evidence graph', async () => {
    const user = userEvent.setup()

    render(<KnowledgeGraphPage />)

    await screen.findByTestId('graph-visualization')
    await user.click(screen.getByRole('button', { name: '选择节点' }))

    expect(await screen.findByTestId('node-detail-dialog')).toHaveTextContent('relations:关联')
    expect(screen.getByTestId('node-detail-dialog')).toHaveTextContent('paragraphs:1')

    await user.click(screen.getByRole('button', { name: '切到证据视图' }))

    await waitFor(() => {
      expect(screen.queryByTestId('node-detail-dialog')).not.toBeInTheDocument()
    })

    await waitFor(() => {
      expect(screen.getByTestId('graph-visualization')).toHaveTextContent('nodes:3,edges:2')
    })
  })

  it('loads edge detail with predicates and support paragraphs', async () => {
    const user = userEvent.setup()

    render(<KnowledgeGraphPage />)

    await screen.findByTestId('graph-visualization')
    await user.click(screen.getByRole('button', { name: '选择边' }))

    expect(await screen.findByTestId('edge-detail-dialog')).toHaveTextContent('predicates:关联')
    expect(screen.getByTestId('edge-detail-dialog')).toHaveTextContent('paragraphs:1')

    await user.click(screen.getByRole('button', { name: '切到证据视图' }))

    await waitFor(() => {
      expect(screen.queryByTestId('edge-detail-dialog')).not.toBeInTheDocument()
    })
  })

  it('opens paragraph detail from initial paragraph hash', async () => {
    render(<KnowledgeGraphPage initialParagraphHash="p-1" />)

    await waitFor(() => {
      expect(memoryApi.getMemoryGraphParagraphDetail).toHaveBeenCalledWith('p-1')
    })
    expect(screen.getByRole('tab', { name: '证据视图' })).toHaveAttribute('data-state', 'active')
    expect(await screen.findByTestId('paragraph-detail-dialog')).toHaveTextContent('paragraph:p-1')
  })

  it('returns to entity graph when a directly opened paragraph is deleted', async () => {
    const user = userEvent.setup()
    vi.mocked(memoryApi.getMemoryGraphParagraphDetail)
      .mockResolvedValueOnce({
        success: true,
        paragraph: {
          hash: 'p-1',
          content: 'Alpha 提到了 Beta',
          preview: 'Alpha 提到了 Beta',
          source: 'demo',
          entity_count: 2,
          relation_count: 1,
          entities: ['Alpha', 'Beta'],
          relations: ['alpha 关联 beta'],
        },
        evidence_graph: {
          nodes: [{ id: 'paragraph:p-1', type: 'paragraph', content: 'Alpha 提到了 Beta', metadata: { hash: 'p-1' } }],
          edges: [],
          focus_entities: ['Alpha', 'Beta'],
        },
      })
      .mockRejectedValueOnce(new Error('paragraph missing'))

    render(<KnowledgeGraphPage initialParagraphHash="p-1" />)

    expect(await screen.findByTestId('paragraph-detail-dialog')).toHaveTextContent('paragraph:p-1')
    await user.click(screen.getByRole('button', { name: '删除这段证据' }))
    await screen.findByTestId('memory-delete-dialog')
    await user.click(screen.getByRole('button', { name: '执行删除' }))

    await waitFor(() => {
      expect(memoryApi.executeMemoryDelete).toHaveBeenCalled()
      expect(screen.getByRole('tab', { name: '实体关系图' })).toHaveAttribute('data-state', 'active')
    })
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '已刷新图谱',
      }),
    )
  })

  it('opens delete preview dialog from node detail', async () => {
    const user = userEvent.setup()

    render(<KnowledgeGraphPage />)

    await screen.findByTestId('graph-visualization')
    await user.click(screen.getByRole('button', { name: '选择节点' }))
    await screen.findByTestId('node-detail-dialog')

    await user.click(screen.getByRole('button', { name: '删除实体' }))

    await waitFor(() => {
      expect(memoryApi.previewMemoryDelete).toHaveBeenCalled()
    })
    expect(await screen.findByTestId('memory-delete-dialog')).toHaveTextContent('delete:mixed:1')
  })
})
