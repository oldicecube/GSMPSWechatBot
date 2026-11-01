import { cn } from '@/lib/utils'
import type { ConfigSchema, FieldSchema } from '@/types/config-schema'

export const advancedFieldTitleClassName = 'text-sky-700 dark:text-sky-300'
export const normalFieldTitleClassName = 'text-foreground'

export function isAdvancedField(schema?: ConfigSchema | FieldSchema): boolean {
  return Boolean(schema && 'advanced' in schema && schema.advanced)
}

export function fieldTitleClassName(
  schema: ConfigSchema | FieldSchema | undefined,
  className?: string,
) {
  return cn(
    className,
    isAdvancedField(schema)
      ? advancedFieldTitleClassName
      : normalFieldTitleClassName,
  )
}
