<script setup lang="ts">
import { Input } from '@atlanhq/atlantis'
import type { InputWidget } from '~/types/workflows'

const { property, disabled } = defineProps<{
    property: InputWidget & { ui: Record<string, unknown> }
    disabled?: boolean
}>()

const modelValue = defineModel<string>()

const componentProps = computed(() => property.ui)
</script> 

<template>
        <Input
            v-model="modelValue"
            v-bind="componentProps"
            :disabled="property.BYOCdisabled || disabled"
            variant="default"
            state="idle"
        />
</template>
