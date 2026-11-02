import { ErrorBoundary } from '@/components/error-boundary'
import { PluginCustomPage } from '@/components/plugin-custom-page'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { refreshPluginWebUI, usePluginWebUI } from '@/lib/plugin-webui'

/** Reuse the plugin's sandbox, API bridge and current theme inside its editor. */
export function PluginInlineSettings({ pluginId, pageId }: { pluginId: string; pageId: string }) {
  const registry = usePluginWebUI(true)
  const extension = registry.extensions.find((item) => item.plugin_id === pluginId)
  const page = extension?.pages.find((item) => item.id === pageId)
  if (registry.loading) return <p className="p-4 text-sm text-muted-foreground">正在加载插件设置…</p>
  const error = registry.error || extension?.compatibility_error
    || (!page || page.mode !== 'custom' ? '插件设置页暂不可用，请等待插件加载后刷新。' : '')
  if (error || !page || page.mode !== 'custom') return (
    <Alert className="mt-4">
      <AlertDescription>{error}</AlertDescription>
      <Button className="mt-3" variant="outline" size="sm" onClick={() => void refreshPluginWebUI(true)}>刷新插件设置</Button>
    </Alert>
  )
  return (
    <ErrorBoundary key={`${pluginId}/${pageId}/${page.document_revision}`}>
      <PluginCustomPage pluginId={pluginId} page={page} pages={extension!.pages} />
    </ErrorBoundary>
  )
}
