import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PluginInlineSettings } from '@/components/plugin-inline-settings'
import type { WebUIExtension } from '@/lib/plugin-webui'

const mocks = vi.hoisted(() => ({ registry: { extensions: [] as WebUIExtension[], loading: false, error: null as string | null }, refresh: vi.fn() }))
vi.mock('@/lib/plugin-webui', () => ({ usePluginWebUI: () => mocks.registry, refreshPluginWebUI: mocks.refresh }))
vi.mock('@/components/plugin-custom-page', () => ({ PluginCustomPage: ({ pluginId, page }: { pluginId: string; page: { id: string } }) => (
  <div data-testid="embedded-story" data-plugin={pluginId} data-page={page.id}>标签、导入与授权</div>
) }))

describe('plugin inline configuration', () => {
  beforeEach(() => { mocks.registry = { extensions: [], loading: false, error: null } })
  it('renders its registered page inside its owner even when navigation is hidden', () => {
    mocks.registry.extensions = [{ schema_version: 2, plugin_id: 'owner', config_page_id: 'story', workspace_title: 'Story',
      pages: [{ id: 'story', mode: 'custom', title: 'Story', description: '', placement: 'workspace', icon: 'settings',
        entry: 'story.html', document_revision: '1', layout: 'document', queries: {}, actions: {}, uploads: {} }] }]
    render(<PluginInlineSettings pluginId="owner" pageId="story" />)
    expect(screen.getByTestId('embedded-story')).toHaveAttribute('data-plugin', 'owner')
    expect(screen.getByTestId('embedded-story')).toHaveAttribute('data-page', 'story')
  })
  it('does not render a different plugin page and provides recovery', () => {
    render(<PluginInlineSettings pluginId="missing" pageId="story" />)
    expect(screen.queryByTestId('embedded-story')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新插件设置' }))
    expect(mocks.refresh).toHaveBeenCalledWith(true)
  })
  it('shows registry errors without a blank configuration pane', () => {
    mocks.registry.error = '连接中断'
    render(<PluginInlineSettings pluginId="owner" pageId="story" />)
    expect(screen.getByText('连接中断')).toBeInTheDocument()
  })
})
