export function getDefaultValuesFromConfigMap(
    configMap: Record<string, unknown>
) {
    let defaultValues = {}

    Object.entries(configMap.properties).forEach(([key, value]) => {
        if (value.default) {
            defaultValues[key] = value.default
        } else if (value.properties) {
            defaultValues[key] = getDefaultValuesFromConfigMap(value)
        }
    })

    return defaultValues
}
