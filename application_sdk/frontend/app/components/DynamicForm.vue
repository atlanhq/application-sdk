<script setup lang="ts">
    import { Label } from '@atlanhq/atlantis'
    import { getFieldValidators } from '~/utils/getFieldValidators'
    import { FormInjectionKey } from '~/constants/workflows'
    import { useForm } from '@tanstack/vue-form'
    import { crush } from 'radash'

    const { configMap, baseKey, currentStep } = defineProps<{
        configMap: Record<string, unknown>
        baseKey?: string
        currentStep?: {}
    }>()

    const form = inject(FormInjectionKey) as ReturnType<typeof useForm>

    const formState = form.useStore((state) => crush(state.values))

    const getCol = (grid: number = 8, start, end) => {
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
                if (currentStep?.properties) {
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
                case 'number':
                    return resolveComponent('Input')
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
                    return resolveComponent('Input')
            }
        } else {
            switch (property.ui?.widget) {
                case 'divider':
                    return 'a-divider'
                case 'header':
                    return 'h5'
                case 'credential':
                    return resolveComponent('Credential')
                case 'nested':
                    return resolveComponent('Nested')
                case 'input':
                    return resolveComponent('Input')
                case 'select':
                    return resolveComponent('Select')
                default:
                    return property.ui.widget
            }
        }
    }

    async function validate() {
        await form.handleSubmit()

        if (!form.state.isFormValid) {
            throw new Error('Form is invalid')
        }

        return form.state.values
    }

    defineExpose({
        validate,
    })
</script>

<template>
    <div class="grid grid-cols-8 gap-x-4 gap-y-4">
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
                <form.Field
                    v-if="compoundWidgets.includes(property?.ui?.widget)"
                    :name="property.id"
                    :validators="getFieldValidators(property)"
                >
                    <template v-slot="{ field }">
                        <Component
                            :is="componentName(property)"
                            :field="field"
                            :property="property"
                        />

                        <span
                            v-if="
                                field.state.meta.isTouched &&
                                !field.state.meta.isValid
                            "
                            class="text-red-500 text-xs"
                        >
                            {{ field.state.meta.errors.join(', ') }}
                        </span>
                    </template>
                </form.Field>

                <form.Field
                    v-else
                    :name="property.id"
                    :validators="getFieldValidators(property)"
                >
                    <template v-slot="{ field }">
                        <Label v-if="property.ui?.label">
                            {{ property.ui?.label }}

                            <span
                                v-if="property?.required"
                                class="text-red-500"
                            >
                                *
                            </span>
                        </Label>

                        <Component
                            :is="componentName(property)"
                            :field="field"
                            :baseKey="baseKey"
                            :property="property"
                        />

                        <span
                            v-if="
                                field.state.meta.isTouched &&
                                !field.state.meta.isValid
                            "
                            class="text-red-500 text-xs"
                        >
                            {{ field.state.meta.errors.join(', ') }}
                        </span>
                    </template>
                </form.Field>
            </div>
        </template>
    </div>
</template>
