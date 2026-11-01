type ApiErrorDetail = {
  loc?: unknown
  msg?: unknown
  message?: unknown
  type?: unknown
}

/** 将 FastAPI 校验错误中的 loc 路径转换为可读字段路径。 */
function formatLocation(loc: unknown): string {
  if (Array.isArray(loc)) {
    return loc.map((item) => String(item)).join('.')
  }
  if (loc === null || loc === undefined || loc === '') {
    return ''
  }
  return String(loc)
}

/** 将单个错误详情转换为可安全渲染的字符串。 */
function formatDetailItem(item: unknown): string {
  if (typeof item === 'string') {
    return item
  }

  if (item && typeof item === 'object') {
    const detail = item as ApiErrorDetail
    const message = detail.msg ?? detail.message
    const location = formatLocation(detail.loc)
    if (message !== null && message !== undefined && message !== '') {
      return location ? `${location}: ${String(message)}` : String(message)
    }
  }

  try {
    return JSON.stringify(item)
  } catch {
    return String(item)
  }
}

/** 判断候选错误字段是否包含可展示的信息。 */
function hasUsableMessage(value: unknown): boolean {
  if (value === null || value === undefined || value === '') {
    return false
  }
  if (Array.isArray(value)) {
    return value.length > 0
  }
  return true
}

/**
 * 将后端错误响应统一转换为字符串，避免将对象直接传给 React 渲染。
 */
export function formatApiError(errorData: unknown, fallback: string): string {
  if (!errorData) {
    return fallback
  }

  if (typeof errorData === 'string') {
    return errorData || fallback
  }

  if (typeof errorData !== 'object') {
    return String(errorData) || fallback
  }

  const data = errorData as { detail?: unknown; message?: unknown; error?: unknown }
  const rawMessage = [data.detail, data.message, data.error].find(hasUsableMessage)

  if (Array.isArray(rawMessage)) {
    const message = rawMessage.map(formatDetailItem).filter(Boolean).join('; ')
    return message || fallback
  }

  if (rawMessage && typeof rawMessage === 'object') {
    const message = formatDetailItem(rawMessage)
    return message || fallback
  }

  if (rawMessage !== null && rawMessage !== undefined && rawMessage !== '') {
    return String(rawMessage)
  }

  return fallback
}
