<script setup lang="ts">
import { computed } from 'vue'
import type { NestedWidget } from '~/types/workflows'

const { property, propertyId } = defineProps<{
    property: NestedWidget
    propertyId: string
}>()

const configMap = computed(() => ({
    properties: property
}))
</script>

<template>
        <DynamicForm 
            :config-map="configMap" 
            :base-key="propertyId" 
        />
</template>
