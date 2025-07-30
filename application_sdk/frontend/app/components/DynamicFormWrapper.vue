<script setup lang="ts">
    import { useForm } from '@tanstack/vue-form'
    import { FormInjectionKey } from '~/constants/workflows'
    import { getDefaultValuesFromConfigMap } from '~/utils/getDefaultValuesFromConfigMap'

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
        return formRefEl.value.validate()
    }

    defineExpose({
        validate,
    })
</script>

<template>
    <DynamicForm ref="formRef" v-bind="props" />
</template>
