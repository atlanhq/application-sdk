export const buildCredentialBody = (
    formState,
    propertyId,
    configName,
    name
) => {
    console.log('formState', formState)
    const extra = {}
    const authType = formState[`${propertyId}.auth-type`]
    const connector = formState[`${propertyId}.connector`]
    const connectorType = formState[`${propertyId}.connectorType`]
    const credentialType = formState[`${propertyId}.credentialType`]
    Object.keys(formState).forEach((key) => {
        if (key.includes(`${propertyId}.extra.`)) {
            extra[key.replace(`${propertyId}.extra.`, '')] = formState[key]
        }
    })

    Object.keys(formState).forEach((key) => {
        if (key.includes(`${propertyId}.${authType}.extra.`)) {
            extra[key.replace(`${propertyId}.${authType}.extra.`, '')] =
                formState[key]
        }
    })

    const computedHost = formState[`${propertyId}.host`]?.endsWith('/')
        ? formState[`${propertyId}.host`].substr(
              0,
              formState[`${propertyId}.host`].length - 1
          )
        : formState[`${propertyId}.host`]
    const computedPort = formState[`${propertyId}.host`]?.startsWith('http')
        ? formState[`${propertyId}.host`].startsWith('https')
            ? 443
            : 80
        : formState[`${propertyId}.port`]

    const hasHost = typeof computedHost === 'string' && computedHost.length > 0
    const hasCredentialType =
        typeof credentialType === 'string' && credentialType.length > 0

    return {
        name,
        ...(hasCredentialType && { credentialType }),
        ...(hasHost && { host: computedHost }),
        ...(hasHost &&
            computedPort && {
                port: formState[`${propertyId}.port`]
                    ? parseInt(formState[`${propertyId}.port`])
                    : computedPort,
            }),
        authType,
        connectorType,
        ...(connector ? { connector } : {}),
        connectBy: formState[`${propertyId}.connectBy`],
        username: formState[`${propertyId}.${authType}.username`],
        password: formState[`${propertyId}.${authType}.password`],
        extra,
        connectorConfigName: configName,
    }
}
