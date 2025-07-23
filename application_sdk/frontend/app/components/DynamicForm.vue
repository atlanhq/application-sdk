<script setup lang="ts">
    import { FormStateInjectionKey } from '~/constants/workflows'
    import { Label } from '@atlanhq/atlantis'
    import { sift, construct } from 'radash'

    const { configMap, baseKey, currentStep } = defineProps<{
        configMap: Record<string, unknown>
        baseKey?: string
        currentStep?: {}
    }>()

    const modelValue = defineModel({ default: {} })
    const formState = inject(FormStateInjectionKey)

    const getCol = (grid: number, start, end) => {
        let c = `col-span-${grid}`
        if (start) c += ` col-start-${start}`
        if (end) c += ` col-start-${start}`

        return c
    }

    const getName = (name: string) => {
        if (baseKey) {
            return `${baseKey}.${name}`
        }

        return name
    }

    const finalConfigMap = computed(() => {
        const clonedConfigMap = JSON.parse(JSON.stringify(configMap))

        if (clonedConfigMap.anyOf) {
            clonedConfigMap.anyOf.forEach((item) => {
                item.required.forEach((i) => {
                    if (
                        clonedConfigMap.properties &&
                        clonedConfigMap.properties[i]
                    ) {
                        clonedConfigMap.properties[i].ui.hidden = true
                    }
                })
            })

            const findMatch = []

            clonedConfigMap.anyOf.forEach((item) => {
                const loopStop = Object.keys(item.properties).every((i) => {
                    if (
                        formState.value[getName(i)]?.toString() ===
                        item.properties[i]?.const?.toString()
                    ) {
                        return true
                    }
                })
                if (loopStop) {
                    findMatch.push(item)
                }
            })

            if (findMatch.length > 0) {
                findMatch.forEach((i) => {
                    i.required.forEach((i) => {
                        if (
                            clonedConfigMap.properties &&
                            clonedConfigMap.properties[i]
                        ) {
                            clonedConfigMap.properties[i].ui.hidden = false
                        }
                    })
                })
            }
        }

        return clonedConfigMap
    })

    const getPropertyFromConfigMap = (key: string) => {
        const property = finalConfigMap.value?.properties[key]

        let baseObj = {
            id: getName(key),
            name: getName(key),
        }

        if (property.type === 'conditional') {
            const { conditions, ...rest } = property
            const matchingCondition = conditions?.find((condition) => {
                if (condition.filter) {
                    const sanitisedFormValue = {} as Record<string, unknown>
                    const allKeys = Object.keys(formState.value)

                    Object.entries(formState.value).forEach(
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
                            sift(condition.filter)
                        ).length > 0
                    )
                }

                if (condition.regex) {
                    return new RegExp(condition.regex).test(
                        formState.value[condition.property]
                    )
                }

                return formState.value[condition.property] === condition.value
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
        {
            const temp = []

            if (finalConfigMap.value?.properties) {
                if (currentStep.properties) {
                    currentStep.properties.forEach((key) => {
                        const found = Object.keys(
                            finalConfigMap?.value?.properties
                        ).find((k) => k === key)
                        if (found) {
                            if (
                                !finalConfigMap.value?.properties[key].ui
                                    ?.hidden
                            ) {
                                temp.push(getPropertyFromConfigMap(key))
                            }
                        }
                    })
                } else {
                    Object.keys(finalConfigMap?.value?.properties).forEach(
                        (key) => {
                            if (
                                !finalConfigMap.value?.properties[key].ui
                                    ?.hidden
                            ) {
                                temp.push(getPropertyFromConfigMap(key))
                            }
                        }
                    )
                }
            }

            return temp
        }
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

    const componentName = (property) => {
        if (!property.ui?.widget) {
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
            switch (property.ui?.widget) {
                case 'divider':
                    return 'a-divider'
                case 'header':
                    return 'h5'
                default:
                    return property.ui.widget
            }
        }
    }
</script>

<template>
    <div class="grid grid-cols-8 gap-x-4">
        <template v-for="property in list" :key="`${property.id}`">
            <div
                v-if="!property.ui?.hidden"
                :class="
                    getCol(
                        property.ui?.grid,
                        property.ui?.start,
                        property.ui?.end
                    )
                "
            >
                <!-- Compound Widgets -->
                <Component
                    :is="componentName(property)"
                    v-if="compoundWidgets.includes(componentName(property))"
                    v-model="modelValue[property.id]"
                    :property="property"
                />

                <!-- Static Widgets -->
                <Component
                    :is="componentName(property)"
                    v-else-if="staticWidgets.includes(property.ui?.widget)"
                    :class="property.ui?.class"
                    :style="property.ui?.style"
                    :property="property"
                >
                    {{ property.ui?.label }}
                </Component>

                <div v-else>
                    <Label v-if="property.ui?.label">
                        {{ property.ui?.label }}
                    </Label>

                    <Component
                        :is="componentName(property)"
                        v-model="modelValue[property.id]"
                        :baseKey="baseKey"
                        :property="property"
                        :disabled="property.ui?.disabled"
                    />
                </div>
            </div>
        </template>
    </div>
</template>
