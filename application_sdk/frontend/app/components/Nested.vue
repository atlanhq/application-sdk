<script setup lang="ts">
    const FormItem = defineAsyncComponent(
        () => import('~/components/DynamicForm.vue')
    )

    const { property } = defineProps<{
        property: Record<string, unknown>
        modelValue: string | undefined
    }>()
</script>

<template>
    <FormItem :configMap="property" :baseKey="property.id" />
</template>
