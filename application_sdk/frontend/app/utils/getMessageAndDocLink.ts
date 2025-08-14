import { isValidJSON } from '~/utils/isValid'

/**
 * Given a successful response message, usually from preflight check endpoints, this function will parse and return
 * a `message` and a `link` to the corresponding troubleshooting docs.
 *
 * If we're unable to parse the message, we'll just return the original message as-is.
 * @param content
 */
export function getMessageAndDocLink(content: string | undefined): {
    message: string | null | undefined
    link: string | null | undefined
} {
    if (!content) {
        return {
            message: null,
            link: null,
        }
    }

    if (!isValidJSON(content)) {
        return {
            message: content,
            link: null,
        }
    }

    const parsedMessage = JSON.parse(content)

    return {
        message:
            parsedMessage.error_message ??
            parsedMessage.debug_message ??
            content,
        link: parsedMessage.doc_link ? parsedMessage.doc_link : null,
    }
}
