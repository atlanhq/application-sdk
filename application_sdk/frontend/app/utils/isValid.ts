export function isValidArray(res) {
    if (Array.isArray(res)) {
        return true
    }
    return false
}

export const isValidJSON = (payload: string) => {
    try {
        const parsed = JSON.parse(payload)
        // Ensure the parsed result is an object or array
        return typeof parsed === 'object' && parsed !== null
    } catch (e) {
        return false
    }
    return true
}
