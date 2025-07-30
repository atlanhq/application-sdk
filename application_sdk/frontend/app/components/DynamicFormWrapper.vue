<script setup lang="ts">
    import { useForm } from '@tanstack/vue-form'
    import { FormInjectionKey } from '~/constants/workflows'
    import { getDefaultValuesFromConfigMap } from '~/utils/getDefaultValuesFromConfigMap'
    import { pick } from 'radash'

    const props = defineProps<{
        configMap: Record<string, unknown>
        baseKey?: string
        currentStep?: {}
    }>()

    const form = useForm({
        defaultValues: getDefaultValuesFromConfigMap(props.configMap),
    })

    const formRefEl = useTemplateRef('formRef')

    provide(FormInjectionKey, form)

    async function validate() {
        try {
            const formValues = await formRefEl.value.validate()

            return props.currentStep
                ? pick(formValues, [
                      ...props.currentStep.properties,
                      'credential_guid_body',
                  ])
                : formValues
        } catch (_error) {
            throw _error
        }
    }

    defineExpose({
        validate,
    })
</script>

<template>
    <DynamicForm ref="formRef" v-bind="props" />
</template>
