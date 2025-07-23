export interface AuthCredentials {
    host: string
    port: string
    database: string
    sqlalchemyUrl: string
    username: string
    password: string
    accessKeyId: string
    secretAccessKey: string
    region: string
    roleArn: string
    externalId: string
}

export interface MetadataOptions {
    include: Map<string, Set<string>>
    exclude: Map<string, Set<string>>
}

export interface PreflightCheck {
    type: string
    success: boolean
    successMessage?: string
    failureMessage?: string
}

export interface StepperItem {
    value: string
    label: string
    title: string
}

export interface SegmentedOption {
    value: string
    label: string
    disabled?: boolean
}
