<script setup lang="ts">
    import { FormStateInjectionKey } from '~/constants/workflows'
    import { Label, Heading } from '@atlanhq/atlantis'
    import { sift, construct } from 'radash'

    const { configMap, baseKey, currentStep } = defineProps<{
        configMap: Record<string, unknown>
        baseKey?: string
        currentStep?: Record<string, unknown>
    }>()

    const modelValue = defineModel<Record<string, unknown>>({ default: {} })
    const formState = inject(FormStateInjectionKey)

    const getCol = (grid = 8, start?: number, end?: number) => {
        let _grid = grid

        if (!grid) {
            _grid = 8
        }

        let c = `col-span-${_grid}`
        if (start) c += ` col-start-${start}`
        if (end) c += ` col-end-${end}`

        return c
    }

    const getName = (name: string) => {
        if (baseKey) {
            return `${baseKey}.${name}`
        }

        return name
    }

    const setDefaultValue = () => {
        if (configMap?.properties && formState?.value) {
            const properties = configMap.properties as Record<string, Record<string, unknown>>
            Object.keys(properties).forEach((key) => {
                const property = properties[key]
                if (property) {
                    const propertyName = getName(key)
                    
                    if (formState.value && [undefined, null].includes(formState.value[propertyName] as null | undefined)) {
                        let defaultValue
                        if (['boolean', 'number'].includes(property.type as string)) {
                            defaultValue = property.default
                        } else {
                            defaultValue = property.default?.toString()
                        }
                        
                        // Set in formState
                        formState.value[propertyName] = defaultValue
                        
                        // Also set in modelValue for Atlantis components
                        if (modelValue.value && defaultValue !== undefined) {
                            modelValue.value[propertyName] = defaultValue
                        }
                    }
                }
            })
        }
    }

    // Use Nuxt lifecycle hook instead of Vue's onBeforeMount
    onMounted(() => {
        setDefaultValue()
    })

    const finalConfigMap = computed(() => {
        const clonedConfigMap = JSON.parse(JSON.stringify(configMap))

        if (clonedConfigMap.anyOf) {
            clonedConfigMap.anyOf.forEach((item: Record<string, unknown>) => {
                const required = item.required as string[]
                required.forEach((i: string) => {
                    if (
                        clonedConfigMap.properties &&
                        clonedConfigMap.properties[i]
                    ) {
                        const property = clonedConfigMap.properties[i] as Record<string, unknown>
                        property.ui = {
                            ...(property.ui as Record<string, unknown> || {}),
                            hidden: true
                        }
                    }
                })
            })

            const findMatch: Record<string, unknown>[] = []

            clonedConfigMap.anyOf.forEach((item: Record<string, unknown>) => {
                const properties = item.properties as Record<string, unknown>
                const loopStop = Object.keys(properties).every((i: string) => {
                    if (
                        formState?.value?.[getName(i)]?.toString() ===
                        (properties[i] as Record<string, unknown>)?.const?.toString()
                    ) {
                        return true
                    }
                    return false
                })
                if (loopStop) {
                    findMatch.push(item)
                }
            })

            if (findMatch.length > 0) {
                findMatch.forEach((i: Record<string, unknown>) => {
                    const required = i.required as string[]
                    required.forEach((i: string) => {
                        if (
                            clonedConfigMap.properties &&
                            clonedConfigMap.properties[i]
                        ) {
                            const property = clonedConfigMap.properties[i] as Record<string, unknown>
                            property.ui = {
                                ...(property.ui as Record<string, unknown> || {}),
                                hidden: false
                            }
                        }
                    })
                })
            }
        }

        return clonedConfigMap
    })

    const getPropertyFromConfigMap = (key: string) => {
        const property = finalConfigMap.value?.properties?.[key] as Record<string, unknown>

        let baseObj = {
            id: getName(key),
            name: getName(key),
        }

        if (property?.type === 'conditional') {
            const { conditions, ...rest } = property
            const matchingCondition = (conditions as Record<string, unknown>[])?.find((condition: Record<string, unknown>) => {
                if (condition.filter) {
                    const sanitisedFormValue = {} as Record<string, unknown>
                    const allKeys = Object.keys(formState?.value || {})

                    Object.entries(formState?.value || {}).forEach(
                        ([_key, _value]) => {
                            if (
                                _key.includes('.') ||
                                allKeys.every((objectKey) =>
                                    objectKey === _key
                                        ? true
                                        : !objectKey.startsWith(_key)
                                )
                            ) {
                                sanitisedFormValue[_key] = _value
                            }
                        }
                    )

                    return (
                        [construct(sanitisedFormValue)].filter(
                            (item) => (sift as unknown as (filter: Record<string, unknown>) => (item: unknown) => boolean)(condition.filter as Record<string, unknown>)(item)
                        ).length > 0
                    )
                }

                if (condition.regex) {
                    return new RegExp(condition.regex as string).test(
                        formState?.value?.[condition.property as string] as string
                    )
                }

                return formState?.value?.[condition.property as string] === condition.value
            })

            baseObj = {
                ...baseObj,
                ...rest,
                ...matchingCondition,
            }
        } else {
            baseObj = { ...baseObj, ...property }
        }

        return baseObj
    }

    const list = computed(() => {
        const temp: Record<string, unknown>[] = []

        if (finalConfigMap.value?.properties) {
            if (currentStep?.properties) {
                const properties = currentStep.properties as string[]
                properties.forEach((key: string) => {
                    const found = Object.keys(
                        finalConfigMap?.value?.properties || {}
                    ).find((k) => k === key)
                    if (found) {
                        const property = finalConfigMap.value?.properties?.[key] as Record<string, unknown>
                        if (!(property?.ui as Record<string, unknown>)?.hidden) {
                            temp.push(getPropertyFromConfigMap(key))
                        }
                    }
                })
            } else {
                Object.keys(finalConfigMap?.value?.properties || {}).forEach(
                    (key: string) => {
                        const property = finalConfigMap.value?.properties?.[key] as Record<string, unknown>
                        if (!(property?.ui as Record<string, unknown>)?.hidden) {
                            temp.push(getPropertyFromConfigMap(key))
                        }
                    }
                )
            }
        }

        return temp
    })

    const staticWidgets = [
        'header',
        'divider',
        'evaluate',
        'FivetranPrerequisites',
        'FivetranNoSupportedConnections',
        'CloudProvider',
        'InfoBanner',
        'FivetranDeprecationWarning',
    ]

    const compoundWidgets = [
        'credential',
        'agent',
        'nested',
        'connection',
        'zeroTouchAI',
        'NestedCollapse',
        'ThresholdInput',
        'DsnTreeMap',
        'JDBCUrlGroup',
    ]

    const componentName = (property: Record<string, unknown>): string => {
        if (!(property.ui as Record<string, unknown>)?.widget) {
            switch (property.type) {
                case 'string':
                    return 'Input'
                case 'number':
                    return 'InputNumber'
                case 'boolean':
                    return 'Boolean'
                case 'array':
                    return 'Select'
                case 'object':
                    return 'Form'
                case 'code':
                    return 'CodeBlock'
                case 'banner':
                    return 'Banner'
                default:
                    return 'Input'
            }
        } else {
            switch ((property.ui as Record<string, unknown>)?.widget) {
                case 'divider':
                    return 'a-divider'
                case 'header':
                    return 'h5'
                default:
                    return (property.ui as Record<string, unknown>).widget as string
            }
        }
    }
</script>

<template>
    <div class="grid grid-cols-8 gap-x-4">
        <template v-for="property in list" :key="`${property.id}`">
            <div
                v-if="!(property.ui as Record<string, unknown>)?.hidden"
                :class="
                    getCol(
                        (property.ui as Record<string, unknown>)?.grid as number,
                        (property.ui as Record<string, unknown>)?.start as number,
                        (property.ui as Record<string, unknown>)?.end as number
                    )
                "
            >
                <!-- Compound Widgets -->
                <Component
                    :is="componentName(property)"
                    v-if="compoundWidgets.includes(componentName(property))"
                    v-model="modelValue[(property.id as string)]"
                    :property="property"
                />

                <!-- Static Widgets -->
                <Component
                    :is="componentName(property)"
                    v-else-if="staticWidgets.includes((property.ui as Record<string, unknown>)?.widget as string)"
                    :class="(property.ui as Record<string, unknown>)?.class"
                    :style="(property.ui as Record<string, unknown>)?.style"
                    :property="property"
                >
                    {{ (property.ui as Record<string, unknown>)?.label }}
                </Component>

                <div v-else>
                    <!-- Render heading or label -->
                    <Heading 
                        v-if="(property.ui as Record<string, unknown>)?.heading" 
                        as="h2" 
                        size="lg" 
                        class="mb-4 col-span-8"
                    >
                        {{ (property.ui as Record<string, unknown>)?.heading }}
                    </Heading>
                    
                    <Label v-else-if="(property.ui as Record<string, unknown>)?.label">
                        {{ (property.ui as Record<string, unknown>)?.label }}
                    </Label>

                    <Component
                        :is="componentName(property)"
                        v-model="modelValue[(property.id as string)]"
                        :base-key="baseKey"
                        :property="property"
                        :disabled="(property.ui as Record<string, unknown>)?.disabled"
                    />
                </div>
            </div>
        </template>
    </div>
</template>
